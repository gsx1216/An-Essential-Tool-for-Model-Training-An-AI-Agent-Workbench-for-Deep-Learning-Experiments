import axios, { AxiosError, CanceledError } from "axios";
import { consumeChatStream, type ChatStreamEvent } from "../lib/sse";

export class ApiError extends Error {
  constructor(
    message: string,
    public status = 0,
    public requestId = "",
  ) {
    super(message);
    this.name = "ApiError";
  }
}
let token = "";
let generation = 0;
let onUnauthorized: () => void = () => undefined;
export function configureIdentity(value: string, handler?: () => void) {
  token = value;
  generation++;
  if (handler) onUnauthorized = handler;
}
export function identityGeneration() {
  return generation;
}
export const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || "/api",
  timeout: 660_000,
});
http.interceptors.request.use((config) => {
  if (token) config.headers.Authorization = `Bearer ${token}`;
  // Used only inside this client; never added to HTTP headers or persisted.
  Object.assign(config, { identityGeneration: generation });
  return config;
});
http.interceptors.response.use(
  (response) => {
    if (
      (
        response.config as typeof response.config & {
          identityGeneration: number;
        }
      ).identityGeneration !== generation
    )
      throw new CanceledError("身份已变化");
    return response;
  },
  (error: AxiosError<{ detail?: unknown }>) => {
    if (axios.isCancel(error)) return Promise.reject(error);
    if (
      (error.config as typeof error.config & { identityGeneration?: number })
        ?.identityGeneration !== generation
    )
      return Promise.reject(new CanceledError("身份已变化"));
    const status = error.response?.status || 0;
    const path = error.config?.url || "";
    if (
      status === 401 &&
      token &&
      !["/auth/login", "/auth/register"].includes(path)
    )
      onUnauthorized();
    const detail = error.response?.data?.detail;
    const message =
      status >= 500
        ? "服务暂时不可用，请稍后重试。"
        : typeof detail === "string"
          ? detail
          : status === 422
            ? "参数不符合要求，请检查必填项和格式。"
            : status === 0
              ? "连接中断或请求超时，请检查网络。"
              : "请求未完成，请重试。";
    return Promise.reject(
      new ApiError(
        message,
        status,
        String(error.response?.headers["x-request-id"] || ""),
      ),
    );
  },
);
export const isCancelled = (error: unknown) => axios.isCancel(error);
export function errorText(error: unknown) {
  return error instanceof ApiError
    ? `${error.message}${error.requestId ? `（追踪号 ${error.requestId}）` : ""}`
    : error instanceof Error
      ? error.message
      : "操作未完成，请稍后重试。";
}
export async function get<T>(
  url: string,
  params?: object,
  signal?: AbortSignal,
): Promise<T> {
  return (await http.get<T>(url, { params, signal })).data;
}
export async function post<T>(
  url: string,
  body?: unknown,
  params?: object,
): Promise<T> {
  return (await http.post<T>(url, body, { params })).data;
}
export async function put<T>(url: string, body?: unknown): Promise<T> {
  return (await http.put<T>(url, body)).data;
}
export async function del(url: string): Promise<void> {
  await http.delete(url);
}

export async function streamPost(
  url: string,
  body: unknown,
  receive: (event: ChatStreamEvent) => void,
) {
  const identity = identityGeneration();
  const response = await http.post<ReadableStream<Uint8Array>>(url, body, {
    adapter: "fetch",
    responseType: "stream",
  });
  return consumeChatStream(response.data, (event) => {
    if (identity !== identityGeneration())
      throw new CanceledError("身份已变化");
    if (event.type === "error")
      throw new ApiError(
        event.detail || "本轮回复未完成。",
        event.status || 503,
        String(response.headers["x-request-id"] || ""),
      );
    receive(event);
  });
}
