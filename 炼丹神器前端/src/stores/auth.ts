import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "../api";
import { configureIdentity, ApiError, errorText } from "../api/client";
import type { User } from "../types/domain";
const STORAGE_KEY = "liandan.login";
export const useAuth = defineStore("auth", () => {
  const user = ref<User | null>(null);
  const token = ref("");
  const ready = ref(false);
  const restoreError = ref("");
  function clear() {
    token.value = "";
    user.value = null;
    restoreError.value = "";
    configureIdentity("");
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* Memory-only login is still usable. */
    }
  }
  function install(value: string) {
    token.value = value;
    configureIdentity(value, clear);
  }
  async function restore() {
    restoreError.value = "";
    try {
      if (!token.value) {
        let saved: { token: string; expires: number } | null = null;
        try {
          saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null");
        } catch {
          clear();
        }
        if (
          saved &&
          typeof saved.token === "string" &&
          saved.expires > Date.now()
        )
          install(saved.token);
      }
      if (token.value) user.value = await api.me();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) clear();
      else restoreError.value = errorText(error);
    } finally {
      ready.value = true;
    }
  }
  async function login(username: string, password: string) {
    const response = await api.login({ username, password });
    install(response.access_token);
    user.value = response.user;
    try {
      sessionStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          token: response.access_token,
          expires: Date.now() + response.expires_in * 1000,
        }),
      );
    } catch {
      /* Do not persist secrets elsewhere. */
    }
    ready.value = true;
  }
  async function logout() {
    try {
      await api.logout();
    } finally {
      clear();
    }
  }
  return { user, token, ready, restoreError, restore, login, logout, clear };
});
