<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import {
  Plus,
  Search,
  Document,
  Download,
  RefreshRight,
  Delete,
} from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import { errorText } from "../api/client";
import { useWorkspace } from "../stores/workspace";
import { confirmAction, notifyError } from "../lib/ui";
import { dateTime, shortId } from "../lib/format";
import { itemsOf } from "../types/domain";
import type { Schema } from "../types/domain";
import StatusBadge from "../components/StatusBadge.vue";
import SectionState from "../components/SectionState.vue";
import DataTree from "../components/DataTree.vue";
import MarkdownText from "../components/MarkdownText.vue";
import { useSerialPoll } from "../composables/useSerialPoll";
import { paperError } from "../lib/papers";
const router = useRouter(),
  workspace = useWorkspace();
const tab = ref("papers"),
  papers = ref<Schema<"PaperResponse">[]>([]),
  nextOffset = ref<number | null>(null),
  loading = ref(false),
  error = ref(""),
  uploadOpen = ref(false),
  uploadFile = ref<File | null>(null),
  uploadTitle = ref(""),
  uploadId = ref(""),
  uploadBusy = ref(false),
  uploadError = ref("");
const fileInput = ref<HTMLInputElement>(),
  query = ref(""),
  paperFilter = ref(""),
  searching = ref(false),
  searchError = ref(""),
  hits = ref<Schema<"ChunkHit">[] | null>(null),
  searchNotice = ref("");
const selectedPaper = ref<Schema<"PaperResponse"> | null>(null),
  chunks = ref<Schema<"ChunkHit">[]>([]),
  chunkCursor = ref<number | null>(null),
  chunkBusy = ref(false),
  chunkError = ref(""),
  chunk = ref<Schema<"ChunkResponse"> | null>(null),
  skill = ref<Schema<"SkillSnapshot"> | null>(null),
  paperBusy = ref(false);
const selectedSession = ref(""),
  references = ref<unknown[]>([]),
  advice = ref<unknown[]>([]),
  referenceError = ref(""),
  referencesBusy = ref(false);
const filteredChunk = computed(() => selectedPaper.value?.status === "ready");
const skillOpen = ref(false),
  skillBusy = ref(false),
  skillError = ref("");
const deletingSkill = ref("");
const skillForm = reactive({ title: "", instructions: "" });
const instructionBytes = computed(
  () =>
    new globalThis.TextEncoder().encode(skillForm.instructions.trim()).length,
);
async function createSkill() {
  if (skillBusy.value) return;
  if (
    !skillForm.title.trim() ||
    !skillForm.instructions.trim() ||
    instructionBytes.value > 6144
  ) {
    skillError.value =
      "请填写名称和提示词；提示词最多 6144 个 UTF-8 字节，约 2048 个汉字。";
    return;
  }
  skillBusy.value = true;
  skillError.value = "";
  try {
    const created = await api.createSkill({
      title: skillForm.title.trim(),
      instructions: skillForm.instructions.trim(),
    });
    workspace.skills.push({
      skill_id: created.skill_id,
      title: created.title,
      description: created.description,
      version: created.version,
      sha256: created.sha256,
    });
    skillOpen.value = false;
    skillForm.title = "";
    skillForm.instructions = "";
    ElMessage.success("个人 Skill 已保存，可选择用于新问题");
  } catch (e) {
    skillError.value = errorText(e);
  } finally {
    skillBusy.value = false;
  }
}
async function deleteSkill(item: Schema<"SkillSummary">) {
  if (deletingSkill.value) return;
  if (
    !(await confirmAction(
      `删除个人 Skill“${item.title}”？以后不能再选择，历史问题已使用的版本仍保留。`,
      "删除个人 Skill",
    ))
  )
    return;
  deletingSkill.value = item.skill_id;
  try {
    await api.deleteSkill(item.skill_id);
    workspace.skills = workspace.skills.filter(
      (entry) => entry.skill_id !== item.skill_id,
    );
    if (workspace.selectedSkill === item.skill_id)
      workspace.selectedSkill = null;
    if (skill.value?.skill_id === item.skill_id) skill.value = null;
    ElMessage.success("个人 Skill 已删除");
  } catch (e) {
    notifyError(e);
  } finally {
    deletingSkill.value = "";
  }
}
useSerialPoll(
  () =>
    papers.value.some((p) => p.status === "processing") ||
    selectedPaper.value?.status === "processing",
  async (signal) => {
    const ids = new Set(
      papers.value
        .filter((p) => p.status === "processing")
        .map((p) => p.paper_id),
    );
    if (selectedPaper.value?.status === "processing")
      ids.add(selectedPaper.value.paper_id);
    const results = await Promise.allSettled(
      [...ids].map((id) => api.paper(id)),
    );
    if (signal.aborted) return;
    for (const result of results) {
      if (result.status !== "fulfilled") continue;
      const data = result.value;
      papers.value = papers.value.map((p) =>
        p.paper_id === data.paper_id ? data : p,
      );
      if (selectedPaper.value?.paper_id === data.paper_id) {
        selectedPaper.value = data;
        if (data.status === "ready") await loadChunks();
      }
    }
  },
);
let paperEpoch = 0,
  searchEpoch = 0,
  chunkEpoch = 0;
async function load(more = false) {
  loading.value = true;
  error.value = "";
  try {
    const data = await api.papers(more ? nextOffset.value || 0 : 0);
    papers.value = more ? [...papers.value, ...data.items] : data.items;
    nextOffset.value = data.next_offset;
  } catch (e) {
    error.value = errorText(e);
  } finally {
    loading.value = false;
  }
}
function chooseFile(event: Event) {
  if (uploadId.value) return;
  const file = (event.target as HTMLInputElement).files?.[0];
  uploadError.value = "";
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) || file.size > 10 * 1024 * 1024) {
    uploadError.value = "请选择不超过 10 MiB 的 PDF 文件。";
    return;
  }
  uploadFile.value = file;
  uploadTitle.value ||= file.name.replace(/\.pdf$/i, "").slice(0, 200);
}
async function upload() {
  if (!uploadFile.value || !uploadTitle.value.trim()) {
    uploadError.value = "请选择 PDF 文件并填写标题。";
    return;
  }
  uploadBusy.value = true;
  uploadError.value = "";
  uploadId.value ||= crypto.randomUUID();
  try {
    const data = await api.uploadPaper(
      uploadFile.value,
      uploadTitle.value.trim(),
      uploadId.value,
    );
    uploadOpen.value = false;
    uploadFile.value = null;
    uploadTitle.value = "";
    uploadId.value = "";
    if (fileInput.value) fileInput.value.value = "";
    await load();
    await openPaper(data);
    ElMessage.success(
      data.status === "ready"
        ? "论文已上传并建立索引"
        : "文件已保存，请查看解析状态",
    );
  } catch (e) {
    uploadError.value = errorText(e);
  } finally {
    uploadBusy.value = false;
  }
}
async function search() {
  if (query.value.trim().length < 2) {
    searchError.value = "请输入至少两个字符的关键词。";
    return;
  }
  const current = ++searchEpoch;
  searching.value = true;
  searchError.value = "";
  try {
    const data = await api.searchPaper(
      query.value.trim(),
      paperFilter.value || undefined,
    );
    if (current !== searchEpoch) return;
    hits.value = data.items;
    searchNotice.value = data.notice;
  } catch (e) {
    if (current === searchEpoch) searchError.value = errorText(e);
  } finally {
    if (current === searchEpoch) searching.value = false;
  }
}
async function openPaper(paper: Schema<"PaperResponse">) {
  selectedPaper.value = paper;
  chunks.value = [];
  chunkCursor.value = null;
  chunk.value = null;
  chunkError.value = "";
  paperEpoch++;
  if (paper.status === "ready") await loadChunks();
}
async function loadChunks(more = false) {
  const paper = selectedPaper.value,
    current = paperEpoch;
  if (!paper) return;
  chunkBusy.value = true;
  chunkError.value = "";
  try {
    const data = await api.chunks(
      paper.paper_id,
      more ? chunkCursor.value || 0 : 0,
    );
    if (current !== paperEpoch) return;
    chunks.value = more ? [...chunks.value, ...data.items] : data.items;
    chunkCursor.value = data.next_offset;
  } catch (e) {
    if (current === paperEpoch) chunkError.value = errorText(e);
  } finally {
    if (current === paperEpoch) chunkBusy.value = false;
  }
}
async function readChunk(id: string, offset = 0) {
  const current = ++chunkEpoch;
  chunkBusy.value = true;
  chunkError.value = "";
  try {
    const data = await api.chunk(id, offset);
    if (current === chunkEpoch) chunk.value = data;
  } catch (e) {
    if (current === chunkEpoch) chunkError.value = errorText(e);
  } finally {
    if (current === chunkEpoch) chunkBusy.value = false;
  }
}
async function neighbor(index: number) {
  if (!chunk.value) return;
  try {
    const page = await api.chunks(chunk.value.paper_id, index);
    if (page.items[0]) await readChunk(page.items[0].chunk_id);
  } catch (e) {
    notifyError(e);
  }
}
async function refreshPaper() {
  if (!selectedPaper.value) return;
  try {
    await openPaper(await api.paper(selectedPaper.value.paper_id));
    await load();
  } catch (e) {
    notifyError(e);
  }
}
async function retryPaper(rebuild: boolean) {
  const paper = selectedPaper.value;
  if (!paper) return;
  if (
    rebuild &&
    !(await confirmAction(
      "重建此论文的解析与检索索引？已保存的旧引用仍对应原快照。",
      "重建论文索引",
    ))
  )
    return;
  paperBusy.value = true;
  try {
    await openPaper(await api.retryPaper(paper.paper_id, rebuild));
    await load();
  } catch (e) {
    notifyError(e);
  } finally {
    paperBusy.value = false;
  }
}
async function removePaper(paper: Schema<"PaperResponse"> | null) {
  if (!paper || paperBusy.value) return;
  paperBusy.value = true;
  try {
    if (
      !(await confirmAction(
        `永久删除“${paper.title}”的 PDF 原件、正文分块及检索索引？删除后无法恢复；历史聊天和已保存的引用快照会保留。`,
        "删除资料",
      ))
    )
      return;
    await api.deletePaper(paper.paper_id);
    if (selectedPaper.value?.paper_id === paper.paper_id) {
      paperEpoch++;
      selectedPaper.value = null;
      chunks.value = [];
    }
    chunkEpoch++;
    chunkBusy.value = false;
    if (chunk.value?.paper_id === paper.paper_id) chunk.value = null;
    searchEpoch++;
    searching.value = false;
    if (paperFilter.value === paper.paper_id) paperFilter.value = "";
    hits.value = null;
    papers.value = papers.value.filter(
      (item) => item.paper_id !== paper.paper_id,
    );
    ElMessage.success("资料原件及正文索引已删除");
    await load();
  } catch (e) {
    notifyError(e);
  } finally {
    paperBusy.value = false;
  }
}
async function download() {
  const paper = selectedPaper.value;
  if (!paper) return;
  paperBusy.value = true;
  try {
    const blob = await api.paperFile(paper.paper_id);
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${paper.title.replace(/[<>:"/\\|?*]/g, "_")}.pdf`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (e) {
    notifyError(e);
  } finally {
    paperBusy.value = false;
  }
}
async function showSkill(id: string) {
  try {
    skill.value = await api.skill(id);
  } catch (e) {
    notifyError(e);
  }
}
async function loadReferences() {
  const id = selectedSession.value;
  references.value = [];
  advice.value = [];
  if (!id) return;
  referencesBusy.value = true;
  referenceError.value = "";
  try {
    const data = await Promise.all([api.references(id), api.advice(id)]);
    if (id !== selectedSession.value) return;
    references.value = itemsOf(data[0]);
    advice.value = itemsOf(data[1]);
  } catch (e) {
    if (id === selectedSession.value) referenceError.value = errorText(e);
  } finally {
    referencesBusy.value = false;
  }
}
watch(selectedSession, loadReferences);
onMounted(() => {
  void load();
  if (!workspace.skills.length) void workspace.loadSkills().catch(notifyError);
});
</script>
<template>
  <div class="page">
    <header class="page-header">
      <div>
        <div class="eyebrow">PAPERS, EVIDENCE & SKILLS</div>
        <h1>资料与建议</h1>
        <p>从论文正文到实验依据，让讨论有来源可循。</p>
      </div>
      <el-button
        v-if="tab === 'skills'"
        type="primary"
        :icon="Plus"
        @click="skillOpen = true"
        >新增个人 Skill</el-button
      >
      <el-button v-else type="primary" :icon="Plus" @click="uploadOpen = true"
        >上传论文</el-button
      >
    </header>
    <el-tabs v-model="tab"
      ><el-tab-pane label="论文资料库" name="papers" /><el-tab-pane
        label="会话来源与建议"
        name="references" /><el-tab-pane label="Skill 工作流" name="skills"
    /></el-tabs>
    <section v-if="tab === 'papers'">
      <form class="toolbar" @submit.prevent="search">
        <el-input
          v-model="query"
          maxlength="180"
          clearable
          placeholder="输入中英文关键词，检索论文正文"
          aria-label="论文检索关键词"
        /><el-select
          v-model="paperFilter"
          clearable
          filterable
          placeholder="全部已加载论文"
          ><el-option
            v-for="paper in papers.filter((p) => p.status === 'ready')"
            :key="paper.paper_id"
            :label="paper.title"
            :value="paper.paper_id" /></el-select
        ><el-button :icon="Search" native-type="submit" :loading="searching"
          >检索</el-button
        ><el-button v-if="hits !== null" text @click="hits = null"
          >返回论文列表</el-button
        ><el-button text @click="load()">刷新列表</el-button>
      </form>
      <p class="fine-print">
        支持不超过 10 MiB、200 页的文本
        PDF；当前使用关键词检索。扫描版或空白页可能没有可提取正文。
      </p>
      <p v-if="searchError" class="inline-error">{{ searchError }}</p>
      <template v-if="hits !== null"
        ><p class="hint">{{ searchNotice }}</p>
        <SectionState
          :empty="!hits.length"
          text="没有找到匹配片段，试试更具体的术语"
        />
        <div class="chunk-list">
          <button
            v-for="hit in hits"
            :key="hit.chunk_id"
            @click="readChunk(hit.chunk_id)"
          >
            <strong>{{ hit.title }}</strong>
            <p class="fine-print">
              物理第 {{ hit.page_number }} 页 · 分块 {{ hit.chunk_index }}
            </p>
            <p>{{ hit.preview }}</p>
            <span class="text-button">读取片段 →</span>
          </button>
        </div></template
      ><template v-else
        ><SectionState
          :loading="loading && !papers.length"
          :error="error"
          :empty="!loading && !error && !papers.length"
          text="上传论文，建立自己的实验资料库"
          @retry="load()"
        />
        <div class="library-grid">
          <article
            v-for="paper in papers"
            :key="paper.paper_id"
            class="card paper-card"
          >
            <div class="card-title">
              <el-icon :size="24" color="#148e8b"><Document /></el-icon
              ><StatusBadge :state="paper.status" />
            </div>
            <h3>{{ paper.title }}</h3>
            <p class="card-meta">
              {{ paper.page_count }} 页 · {{ paper.chunk_count }} 个片段 ·
              {{ (paper.file_bytes / 1024 / 1024).toFixed(2) }} MiB
            </p>
            <p class="fine-print">{{ dateTime(paper.created_at) }}</p>
            <p v-if="paper.error_code" class="inline-error">
              {{ paperError(paper.error_code) }}
            </p>
            <div class="actions">
              <el-button plain @click="openPaper(paper)"
                >查看正文与索引</el-button
              >
              <el-button
                text
                type="danger"
                :icon="Delete"
                :disabled="paperBusy || paper.status === 'processing'"
                :aria-label="`删除资料 ${paper.title}`"
                @click="removePaper(paper)"
                >删除</el-button
              >
            </div>
            <p v-if="paper.status === 'processing'" class="fine-print">
              解析完成后可删除；已中断的任务会在状态刷新后开放重试或删除。
            </p>
          </article>
        </div>
        <div v-if="nextOffset !== null" class="pagination-actions">
          <el-button :loading="loading" @click="load(true)"
            >加载更多论文</el-button
          >
        </div></template
      >
    </section>
    <section v-else-if="tab === 'references'">
      <div class="toolbar">
        <el-select
          v-model="selectedSession"
          filterable
          placeholder="选择一个聊天"
          ><el-option
            v-for="session in workspace.sessions"
            :key="session.session_id"
            :label="session.title"
            :value="session.session_id" /></el-select
        ><el-button :disabled="!selectedSession" @click="loadReferences"
          >刷新</el-button
        >
      </div>
      <SectionState
        :loading="referencesBusy"
        :error="referenceError"
        :empty="!selectedSession"
        text="选择会话，查看其已保存来源与研究建议"
        @retry="loadReferences"
      /><template v-if="selectedSession && !referencesBusy"
        ><div class="card">
          <h2>资料来源</h2>
          <p class="fine-print">
            最多显示此会话最近 50 条已保存来源；检索摘要不等于已阅读正文。
          </p>
          <SectionState
            :empty="!references.length"
            text="此会话暂无已保存的来源"
          /><DataTree :value="references" />
        </div>
        <div class="card">
          <h2>研究建议</h2>
          <SectionState
            :empty="!advice.length"
            text="此会话暂无已保存的研究建议"
          /><DataTree :value="advice" /></div
      ></template>
    </section>
    <section v-else>
      <p class="hint">
        Skill 是带版本的工作流指令。本轮选择后由后端记录快照，重试沿用原版本。
        可以新增个人提示词，仅本人可见；部署的公共 Skill 也会列在这里。
      </p>
      <div class="card-grid">
        <article
          v-for="item in workspace.skills"
          :key="item.skill_id"
          class="card"
        >
          <div class="card-title">
            <h3>{{ item.title }}</h3>
            <el-tag size="small" effect="plain">v{{ item.version }}</el-tag>
          </div>
          <p class="hint">{{ item.description }}</p>
          <div class="actions">
            <el-button @click="showSkill(item.skill_id)">查看指令</el-button
            ><el-button
              type="primary"
              plain
              @click="
                workspace.selectedSkill = item.skill_id;
                router.push('/chat');
              "
              >用于新问题</el-button
            >
            <el-button
              v-if="item.skill_id.startsWith('personal-')"
              type="danger"
              plain
              :aria-label="`删除个人 Skill ${item.title}`"
              :loading="deletingSkill === item.skill_id"
              @click="deleteSkill(item)"
              >删除</el-button
            >
          </div>
        </article>
      </div>
      <SectionState
        :empty="!workspace.skills.length"
        text="暂无可用 Skill，点击“新增个人 Skill”创建自己的工作流"
      />
    </section>
    <el-dialog
      v-model="skillOpen"
      title="新增个人 Skill"
      width="min(94vw, 640px)"
      :close-on-click-modal="!skillBusy"
      :show-close="!skillBusy"
    >
      <el-form label-position="top" :disabled="skillBusy" @submit.prevent>
        <el-form-item label="名称" required
          ><el-input
            v-model="skillForm.title"
            maxlength="80"
            placeholder="例如：伪装目标检测论文分析"
        /></el-form-item>
        <el-form-item label="提示词 / 工作流程" required
          ><el-input
            v-model="skillForm.instructions"
            type="textarea"
            :rows="10"
            maxlength="6144"
            placeholder="说明要分析的问题、执行步骤和输出格式，例如先读取论文方法与代码，再结合实验结果提出有依据的建议。"
        /></el-form-item>
        <p class="fine-print">
          {{ instructionBytes }} / 6144 UTF-8 字节。Skill
          规定分析流程，不会执行其中的脚本或自动授权训练。
        </p>
      </el-form>
      <p v-if="skillError" class="inline-error">{{ skillError }}</p>
      <template #footer
        ><el-button :disabled="skillBusy" @click="skillOpen = false"
          >取消</el-button
        ><el-button type="primary" :loading="skillBusy" @click="createSkill"
          >保存个人 Skill</el-button
        ></template
      >
    </el-dialog>
    <el-dialog
      v-model="uploadOpen"
      title="上传论文 PDF"
      width="560px"
      :close-on-click-modal="!uploadBusy"
      :show-close="!uploadBusy"
      ><el-form
        label-position="top"
        :disabled="uploadBusy || !!uploadId"
        @submit.prevent
        ><el-form-item label="论文标题" required
          ><el-input v-model="uploadTitle" maxlength="200" /></el-form-item
        ><el-form-item label="PDF 文件" required
          ><input
            ref="fileInput"
            type="file"
            accept="application/pdf,.pdf"
            :disabled="uploadBusy || !!uploadId"
            @change="chooseFile"
        /></el-form-item>
        <p v-if="uploadFile" class="hint">
          {{ uploadFile.name }} ·
          {{ (uploadFile.size / 1024 / 1024).toFixed(2) }} MiB
        </p></el-form
      >
      <p v-if="uploadError" class="inline-error">{{ uploadError }}</p>
      <p v-if="uploadId" class="fine-print">
        重试将使用原上传编号与相同文件，避免重复入库。
      </p>
      <template #footer
        ><el-button :disabled="uploadBusy" @click="uploadOpen = false"
          >关闭</el-button
        ><el-button type="primary" :loading="uploadBusy" @click="upload">{{
          uploadId ? "重试原上传请求" : "上传并解析"
        }}</el-button></template
      ></el-dialog
    >
    <el-drawer
      :model-value="!!selectedPaper"
      :title="selectedPaper?.title || '论文'"
      size="min(94vw, 700px)"
      @close="
        selectedPaper = null;
        paperEpoch++;
      "
      ><template v-if="selectedPaper"
        ><div class="actions">
          <StatusBadge :state="selectedPaper.status" /><el-button
            :disabled="paperBusy"
            @click="refreshPaper"
            >刷新状态</el-button
          ><el-button :icon="Download" :loading="paperBusy" @click="download"
            >下载原件</el-button
          ><el-button
            :icon="RefreshRight"
            :disabled="paperBusy"
            @click="retryPaper(selectedPaper.status === 'ready')"
            >{{
              selectedPaper.status === "ready" ? "重建索引" : "重试解析"
            }}</el-button
          ><el-button
            text
            type="danger"
            :icon="Delete"
            :disabled="paperBusy"
            @click="removePaper(selectedPaper)"
            >删除</el-button
          >
        </div>
        <p class="fine-print">
          文档 #{{ shortId(selectedPaper.paper_id) }} ·
          {{ selectedPaper.page_count }} 页 · {{ selectedPaper.chunk_count }} 块
        </p>
        <p v-if="selectedPaper.error_code" class="inline-error">
          {{ paperError(selectedPaper.error_code) }}（{{
            selectedPaper.error_code
          }}）
        </p>
        <p v-if="selectedPaper.empty_pages.length" class="notice warning-note">
          以下物理页没有可提取正文：{{ selectedPaper.empty_pages.join("、") }}
        </p>
        <SectionState
          :loading="chunkBusy"
          :error="chunkError"
          @retry="loadChunks()"
        />
        <div v-if="filteredChunk" class="chunk-list">
          <button
            v-for="item in chunks"
            :key="item.chunk_id"
            @click="readChunk(item.chunk_id)"
          >
            <strong
              >第 {{ item.page_number }} 页 · 片段
              {{ item.chunk_index }}</strong
            >
            <p class="hint">{{ item.preview }}</p></button
          ><el-button
            v-if="chunkCursor !== null"
            :loading="chunkBusy"
            @click="loadChunks(true)"
            >更多片段</el-button
          >
        </div></template
      ></el-drawer
    >
    <el-drawer
      :model-value="!!chunk"
      title="论文正文片段"
      size="min(96vw, 760px)"
      append-to-body
      @close="
        chunk = null;
        chunkEpoch++;
      "
      ><template v-if="chunk"
        ><h2>{{ chunk.title }}</h2>
        <p class="fine-print">
          物理第 {{ chunk.page_number }} 页 · 片段 {{ chunk.chunk_index }} ·
          段内偏移 {{ chunk.offset }}
        </p>
        <p class="fine-print">文件版本 {{ chunk.file_sha256 }}</p>
        <div class="chunk-text">{{ chunk.text }}</div>
        <div class="actions" style="margin-top: 18px">
          <el-button
            v-if="chunk.previous_chunk_index != null"
            :disabled="chunkBusy"
            @click="neighbor(chunk.previous_chunk_index)"
            >上一片段</el-button
          ><el-button
            v-if="chunk.next_offset !== null"
            :loading="chunkBusy"
            @click="readChunk(chunk.chunk_id, chunk.next_offset)"
            >继续读取本片段</el-button
          ><el-button
            v-if="chunk.next_chunk_index !== null"
            :disabled="chunkBusy"
            @click="neighbor(chunk.next_chunk_index)"
            >下一片段</el-button
          ><el-button
            type="primary"
            plain
            @click="
              router.push({
                path: '/chat',
                query: {
                  prompt: `请读取我的论文《${chunk.title}》（paper_id=${chunk.paper_id}）第${chunk.page_number}页相关片段，解释它解决的问题与方法，并给出正文依据。`,
                },
              })
            "
            >在聊天中讨论</el-button
          >
        </div>
        <p v-if="chunkError" class="inline-error">{{ chunkError }}</p></template
      ></el-drawer
    >
    <el-drawer
      :model-value="!!skill"
      title="Skill 工作流"
      size="min(94vw, 720px)"
      @close="skill = null"
      ><template v-if="skill"
        ><h2>{{ skill.title }}</h2>
        <p class="hint">{{ skill.description }}</p>
        <p class="fine-print">v{{ skill.version }} · {{ skill.sha256 }}</p>
        <MarkdownText :content="skill.instructions" /></template
    ></el-drawer>
  </div>
</template>
