import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AxiosError,
  AxiosHeaders,
  type InternalAxiosRequestConfig,
} from "axios";
import { api } from "../src/api";
import {
  ApiError,
  configureIdentity,
  http,
  isCancelled,
} from "../src/api/client";
const originalAdapter = http.defaults.adapter;
afterEach(() => {
  http.defaults.adapter = originalAdapter;
  configureIdentity("");
});
function response(
  config: InternalAxiosRequestConfig,
  data: unknown,
  status = 200,
) {
  return {
    config,
    data,
    status,
    statusText: "",
    headers: new AxiosHeaders({ "x-request-id": "trace-test" }),
  };
}
describe("API 身份和错误语义", () => {
  it("只放 Bearer 到请求头；204 不读取 JSON", async () => {
    configureIdentity("dummy-login");
    http.defaults.adapter = async (config) => {
      expect(config.headers.Authorization).toBe("Bearer dummy-login");
      expect(config.params).toBeUndefined();
      return response(config, undefined, 204);
    };
    expect(await api.saveCredential("sk-FAKE")).toBeUndefined();
    expect(await api.deleteSession("dummy")).toBeUndefined();
  });
  it("503 不退出登录，错误文案不泄露数据库或供应商详情", async () => {
    const clear = vi.fn();
    configureIdentity("dummy", clear);
    http.defaults.adapter = async (config) => {
      throw new AxiosError(
        "mysql password...",
        "",
        config,
        undefined,
        response(config, { detail: "database internal detail" }, 503),
      );
    };
    await expect(api.me()).rejects.toMatchObject({
      status: 503,
      requestId: "trace-test",
      message: "服务暂时不可用，请稍后重试。",
    });
    expect(clear).not.toHaveBeenCalled();
  });
  it("当前身份 401 失效；登录表单的 401 不触发全局清理", async () => {
    const clear = vi.fn();
    configureIdentity("dummy", clear);
    http.defaults.adapter = async (config) => {
      throw new AxiosError(
        "",
        "",
        config,
        undefined,
        response(config, { detail: "请登录" }, 401),
      );
    };
    await expect(
      api.login({ username: "u", password: "bad" }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(clear).not.toHaveBeenCalled();
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(clear).toHaveBeenCalledOnce();
  });
  it("旧账号的迟到成功响应在账号切换后被丢弃", async () => {
    let finish: () => void = () => undefined;
    configureIdentity("old");
    http.defaults.adapter = (config) =>
      new Promise((resolve) => {
        finish = () =>
          resolve(response(config, { user_id: 1, username: "old" }));
      });
    const pending = api.me().catch((error) => error);
    await new Promise((resolve) => setTimeout(resolve, 0));
    configureIdentity("new");
    finish();
    expect(isCancelled(await pending)).toBe(true);
  });
  it("旧账号的迟到 401 不清理新账号", async () => {
    let fail: () => void = () => undefined;
    const clear = vi.fn();
    configureIdentity("old", clear);
    http.defaults.adapter = (config) =>
      new Promise((_, reject) => {
        fail = () =>
          reject(
            new AxiosError(
              "",
              "",
              config,
              undefined,
              response(config, {}, 401),
            ),
          );
      });
    const pending = api.me().catch((e) => e);
    await new Promise((resolve) => setTimeout(resolve, 0));
    configureIdentity("new", clear);
    fail();
    expect(isCancelled(await pending)).toBe(true);
    expect(clear).not.toHaveBeenCalled();
  });
});
describe("接口归属与请求约定", () => {
  it("后台任务、重试、证据都携带其所属 run_id", async () => {
    const paths: string[] = [];
    http.defaults.adapter = async (config) => {
      paths.push(config.url!);
      return response(config, {});
    };
    await api.agentTask("run", "analysis", "task");
    await api.retryAgent("run", "suggestion", "task");
    await api.evidence("run", "task");
    await api.retryEvent("run", "event");
    expect(paths).toEqual([
      "/runs/run/analysis-tasks/task",
      "/runs/run/suggestion-tasks/task/retry",
      "/runs/run/suggestion-tasks/task/evidence",
      "/runs/run/events/event/retry",
    ]);
  });
  it("PDF 使用原始 application/pdf 请求体，不使用 multipart", async () => {
    const file = new File(["%PDF-1.4"], "test.pdf", {
      type: "application/pdf",
    });
    http.defaults.adapter = async (config) => {
      expect(config.data).toBe(file);
      expect(config.headers["Content-Type"]).toBe("application/pdf");
      expect(config.params).toEqual({ title: "测试论文", request_id: "id" });
      return response(config, {});
    };
    await api.uploadPaper(file, "测试论文", "id");
  });
  it("重试原消息时，调用者传入的 request_id 与 Skill 不被覆盖", async () => {
    const sent: unknown[] = [];
    http.defaults.adapter = async (config) => {
      sent.push(JSON.parse(config.data));
      return response(config, { content: "ok" });
    };
    const turn = {
      content: "你好",
      request_id: "same-turn",
      skill_id: "cod-experiment-analysis",
    };
    await api.send("session", turn);
    await api.send("session", turn);
    expect(sent).toEqual([turn, turn]);
  });
});
