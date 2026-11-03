import { defineStore } from "pinia";
import { reactive } from "vue";
import type { Schema } from "../types/domain";
export interface TurnState {
  busy: boolean;
  request: Schema<"SendMessageRequest"> | null;
  error: string;
  resolved: boolean;
  partial: string;
}
export const useChat = defineStore("chat", () => {
  const turns = reactive<Record<string, TurnState>>({});
  const drafts = reactive<Record<string, string>>({});
  function turn(id: string) {
    return (turns[id] ||= {
      busy: false,
      request: null,
      error: "",
      resolved: true,
      partial: "",
    });
  }
  function clear() {
    Object.keys(turns).forEach((id) => delete turns[id]);
    Object.keys(drafts).forEach((id) => delete drafts[id]);
  }
  return { turns, drafts, turn, clear };
});
