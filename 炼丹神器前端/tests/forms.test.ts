import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ElementPlus from "element-plus";
import AuthView from "../src/views/AuthView.vue";
import SettingsView from "../src/views/SettingsView.vue";
import { api } from "../src/api";
vi.mock("../src/api", () => ({
  api: {
    login: vi.fn(),
    register: vi.fn(),
    credential: vi.fn(),
    preference: vi.fn(),
    saveCredential: vi.fn(),
  },
}));
const navigate = vi.hoisted(() => vi.fn());
vi.mock("vue-router", () => ({
  useRoute: () => ({ query: { return: "/chat" } }),
  useRouter: () => ({ replace: navigate }),
}));
beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  setActivePinia(createPinia());
});
const options = () => ({ global: { plugins: [createPinia(), ElementPlus] } });
describe("登录表单回归", () => {
  it("只有一个原生 form，提交走接口，不把账号密码拼进 URL", async () => {
    vi.mocked(api.login).mockResolvedValue({
      access_token: "TEST",
      token_type: "bearer",
      expires_in: 1000,
      user: { user_id: 1, username: "tester" },
    });
    const wrapper = mount(AuthView, options());
    expect(wrapper.findAll("form")).toHaveLength(1);
    await wrapper.get('input[name="username"]').setValue("tester");
    await wrapper.get('input[name="password"]').setValue("TestPass123!");
    const event = new Event("submit", { cancelable: true, bubbles: true });
    wrapper.get("form").element.dispatchEvent(event);
    await flushPromises();
    expect(event.defaultPrevented).toBe(true);
    expect(api.login).toHaveBeenCalledWith({
      username: "tester",
      password: "TestPass123!",
    });
    expect(navigate).toHaveBeenCalledWith("/chat");
    expect(wrapper.get('input[name="password"]').element).toHaveProperty(
      "value",
      "",
    );
    wrapper.unmount();
  });
  it("过短密码不发请求，显示能操作的提示", async () => {
    const wrapper = mount(AuthView, options());
    await wrapper.get('input[name="username"]').setValue("tester");
    await wrapper.get('input[name="password"]').setValue("123");
    await wrapper.get("form").trigger("submit");
    expect(api.login).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("8～32");
    wrapper.unmount();
  });
});
it("保存 Key 收到 204 后清空输入并显示已配置", async () => {
  vi.mocked(api.credential).mockResolvedValue({
    configured: false,
    provider: "deepseek",
  });
  vi.mocked(api.preference).mockResolvedValue({ enabled: false });
  vi.mocked(api.saveCredential).mockResolvedValue(undefined);
  const wrapper = mount(SettingsView, options());
  await flushPromises();
  await wrapper.get('input[autocomplete="new-password"]').setValue("sk-FAKE");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(api.saveCredential).toHaveBeenCalledWith("sk-FAKE");
  expect(wrapper.text()).toContain("已配置");
  expect(wrapper.get("input").element).toHaveProperty("value", "");
  wrapper.unmount();
});
