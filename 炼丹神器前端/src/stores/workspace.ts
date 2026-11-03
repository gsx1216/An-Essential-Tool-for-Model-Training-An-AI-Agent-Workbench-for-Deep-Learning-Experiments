import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "../api";
import type { Session, Skill } from "../types/domain";
export const useWorkspace = defineStore("workspace", () => {
  const sessions = ref<Session[]>([]);
  const skills = ref<Skill[]>([]);
  const selectedSkill = ref<string | null>(null);
  const sessionError = ref("");
  async function loadSessions() {
    sessions.value = await api.sessions();
  }
  async function loadSkills() {
    skills.value = (await api.skills()).items;
  }
  function clear() {
    sessions.value = [];
    skills.value = [];
    selectedSkill.value = null;
    sessionError.value = "";
  }
  return {
    sessions,
    skills,
    selectedSkill,
    sessionError,
    loadSessions,
    loadSkills,
    clear,
  };
});
