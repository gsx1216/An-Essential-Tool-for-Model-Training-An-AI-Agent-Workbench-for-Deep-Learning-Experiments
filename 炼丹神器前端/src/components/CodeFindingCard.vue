<script setup lang="ts">
import type { Schema } from "../types/domain";
defineProps<{
  finding: Schema<"CodeFinding">;
  evidence: Schema<"CodeEvidence">[];
  index: number;
}>();
</script>
<template>
  <article class="card">
    <div class="card-title">
      <h3>{{ index + 1 }}. {{ finding.problem }}</h3>
      <el-tag :type="finding.assessment === 'confirmed' ? 'info' : 'warning'">{{
        finding.assessment === "confirmed" ? "代码检查结论" : "待验证推测"
      }}</el-tag>
    </div>
    <template
      v-for="citation in finding.code"
      :key="citation.evidence_id + ':' + citation.start_line"
    >
      <div
        v-for="source in evidence.filter(
          (item) => item.evidence_id === citation.evidence_id,
        )"
        :key="source.evidence_id"
      >
        <h4>
          {{ source.path }} · 第 {{ citation.start_line }}～{{
            citation.end_line
          }}
          行
        </h4>
        <pre
          class="code-block"
        ><span v-for="line in source.lines.filter(item=>item.number>=citation.start_line&&item.number<=citation.end_line)" :key="line.number" class="source-line"><span class="line-number">{{ line.number }}</span>{{ line.text }}</span></pre>
      </div>
    </template>
    <dl class="data-tree">
      <div>
        <dt>判断与原因</dt>
        <dd>{{ finding.hypothesis }}</dd>
      </div>
      <div>
        <dt>修改方向</dt>
        <dd>{{ finding.change }}</dd>
      </div>
      <div>
        <dt>适用条件</dt>
        <dd>{{ finding.conditions }}</dd>
      </div>
      <div>
        <dt>验证方法</dt>
        <dd>{{ finding.validation }}</dd>
      </div>
    </dl>
  </article>
</template>
