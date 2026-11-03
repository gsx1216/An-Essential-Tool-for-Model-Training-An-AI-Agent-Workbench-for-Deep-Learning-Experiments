import { get, post, put, del, http, streamPost } from "./client";
import type { ChatStreamEvent } from "../lib/sse";
import type {
  Schema,
  Session,
  Message,
  Run,
  Server,
  Experiment,
  Script,
  Skill,
  TrainingTask,
  ToolPage,
  MetricEvent,
  MetricSummary,
} from "../types/domain";
export const api = {
  login: (body: Schema<"LoginRequest">) =>
    post<Schema<"LoginResponse">>("/auth/login", body),
  register: (body: Schema<"RegisterRequest">) =>
    post<Schema<"UserResponse">>("/auth/register", body),
  me: () => get<Schema<"UserResponse">>("/auth/me"),
  logout: () => post<void>("/auth/logout"),
  sessions: (signal?: AbortSignal) =>
    get<Session[]>("/sessions", undefined, signal),
  createSession: (title = "新聊天") =>
    post<Schema<"SessionCreatedResponse">>("/sessions", { title }),
  deleteSession: (id: string) => del(`/sessions/${id}`),
  history: (id: string, after_seq = 0, signal?: AbortSignal) =>
    get<Message[]>(
      `/sessions/${id}/messages`,
      { after_seq, limit: 100 },
      signal,
    ),
  send: (id: string, body: Schema<"SendMessageRequest">) =>
    post<Schema<"ChatReplyResponse">>(`/sessions/${id}/messages`, body),
  retry: (id: string) =>
    post<Schema<"ChatReplyResponse">>(`/sessions/${id}/retry`),
  sendStream: (
    id: string,
    body: Schema<"SendMessageRequest">,
    receive: (event: ChatStreamEvent) => void,
  ) => streamPost(`/sessions/${id}/messages/stream`, body, receive),
  retryStream: (id: string, receive: (event: ChatStreamEvent) => void) =>
    streamPost(`/sessions/${id}/retry/stream`, undefined, receive),
  calls: (id: string, after_id = 0, signal?: AbortSignal) =>
    get<ToolPage>(
      `/sessions/${id}/tool-calls`,
      { after_id, limit: 100 },
      signal,
    ),
  skillAudit: (id: string, turn: string) =>
    get<Schema<"TurnSkillResponse">>(`/sessions/${id}/turns/${turn}/skill`),
  skills: () => get<Schema<"SkillList">>("/skills"),
  skill: (id: string) => get<Skill & { instructions: string }>(`/skills/${id}`),
  createSkill: (body: Schema<"PersonalSkillCreate">) =>
    post<Schema<"SkillSnapshot">>("/skills", body),
  deleteSkill: (id: string) => del(`/skills/${id}`),
  credential: () =>
    get<Schema<"ModelCredentialStatusResponse">>(
      "/me/model-credentials/deepseek",
    ),
  saveCredential: (api_key: string) =>
    put<void>("/me/model-credentials/deepseek", { api_key }),
  deleteCredential: () => del("/me/model-credentials/deepseek"),
  preference: (name: "analysis" | "suggestion") =>
    get<{ enabled: boolean }>(`/me/${name}-settings`),
  savePreference: (name: "analysis" | "suggestion", enabled: boolean) =>
    put<{ enabled: boolean }>(`/me/${name}-settings`, { enabled }),
  servers: (before_id?: string) =>
    get<Schema<"ServerListResponse">>("/servers", { before_id, limit: 100 }),
  server: (id: string) => get<Server>(`/servers/${id}`),
  createServer: (body: Schema<"CreateServerRequest">) =>
    post<Server>("/servers", body),
  deleteServer: (id: string) => del(`/servers/${id}`),
  testServer: (id: string) =>
    post<Schema<"ServerTestResponse">>(`/servers/${id}/test`),
  experiments: (before_id?: string) =>
    get<Schema<"ExperimentListResponse">>("/experiments", {
      before_id,
      limit: 100,
    }),
  experiment: (id: string) => get<Experiment>(`/experiments/${id}`),
  deleteExperiment: (id: string) => del(`/experiments/${id}`),
  createExperiment: (body: Schema<"CreateExperimentRequest">) =>
    post<Experiment>("/experiments", body),
  runs: (id: string, before_id?: string) =>
    get<Schema<"RunListResponse">>(`/experiments/${id}/runs`, {
      before_id,
      limit: 50,
    }),
  run: (id: string, signal?: AbortSignal) =>
    get<Run>(`/runs/${id}`, undefined, signal),
  deleteRun: (id: string) => del(`/runs/${id}`),
  latestRun: () =>
    get<Schema<"LatestTrainingRunResponse">>("/runs/latest-training"),
  createRun: (id: string, body: Schema<"CreateRunRequest">) =>
    post<Schema<"RunCreationResponse">>(`/experiments/${id}/runs`, body),
  scripts: () => get<{ items: Script[] }>("/runner/scripts"),
  tasks: (query = "", offset = 0) =>
    get<{ items: TrainingTask[]; next_offset: number | null }>(
      "/training-tasks",
      { query, offset, limit: 5 },
    ),
  task: (id: string) => get<TrainingTask>(`/training-tasks/${id}`),
  deleteTask: (id: string) => del(`/training-tasks/${id}`),
  createTask: (body: Schema<"TaskRequest">) =>
    post<TrainingTask>("/training-tasks", body),
  projectEnvironments: (server_id: string) =>
    post<Schema<"ProjectEnvironmentResponse">>(
      "/training-projects/environments",
      { server_id },
    ),
  serverResources: (server_id: string) =>
    post<Schema<"ProjectResourceResponse">>("/training-projects/resources", {
      server_id,
    }),
  projectFiles: (server_id: string, path = ".", offset = 0) =>
    post<{
      server_id: string;
      work_dir: string;
      path: string;
      items: { path: string; kind: "directory" | "file" }[];
      next_offset: number | null;
    }>("/training-projects/files", { server_id, path, offset, limit: 25 }),
  start: (id: string, body: Schema<"StartRunRequest">) =>
    post<Schema<"OperationResponse">>(`/runs/${id}/start`, body),
  execution: (id: string, signal?: AbortSignal) =>
    get<Schema<"ExecutionResponse">>(
      `/runs/${id}/execution`,
      undefined,
      signal,
    ),
  refresh: (id: string) =>
    post<Schema<"OperationResponse">>(`/runs/${id}/refresh`),
  stop: (id: string) => post<Schema<"OperationResponse">>(`/runs/${id}/stop`),
  logs: (id: string, offset: number, signal?: AbortSignal) =>
    get<Schema<"OperationResponse">>(
      `/runs/${id}/logs`,
      { offset, limit: 16384 },
      signal,
    ),
  metrics: (id: string, after = -1, signal?: AbortSignal) =>
    get<{ items: MetricEvent[]; next_offset: number | null }>(
      `/runs/${id}/metrics`,
      { after, limit: 500 },
      signal,
    ),
  metricLatest: (id: string, signal?: AbortSignal) =>
    get<{ items: MetricSummary[] }>(
      `/runs/${id}/metrics/latest`,
      undefined,
      signal,
    ),
  syncMetrics: (id: string) =>
    post<Schema<"MetricSyncResponse">>(`/runs/${id}/metrics/sync`),
  metricParser: (id: string) =>
    get<Schema<"MetricParserRequest">>(`/runs/${id}/metrics/parser`),
  saveMetricParser: (id: string, body: Schema<"MetricParserRequest">) =>
    put<Schema<"MetricParserRequest">>(`/runs/${id}/metrics/parser`, body),
  report: (id: string, signal?: AbortSignal) =>
    get<Schema<"ReportResponse">>(`/runs/${id}/report`, undefined, signal),
  generateReport: (id: string) =>
    post<Schema<"ReportResponse">>(`/runs/${id}/report`),
  monitor: (id: string, signal?: AbortSignal) =>
    get<Schema<"MonitorResponse">>(`/runs/${id}/monitor`, undefined, signal),
  retryMonitor: (id: string) => post<unknown>(`/runs/${id}/monitor/retry`),
  events: (id: string, offset = 0) =>
    get<Schema<"EventResponse">[]>(`/runs/${id}/events`, { offset, limit: 20 }),
  retryEvent: (runId: string, id: string) =>
    post<unknown>(`/runs/${runId}/events/${id}/retry`),
  agentTasks: (id: string, offset = 0) =>
    get<Schema<"AgentTaskResponse">[]>(`/runs/${id}/agent-tasks`, {
      offset,
      limit: 20,
    }),
  analyses: (id: string, offset = 0) =>
    get<Schema<"AnalysisResponse">[]>(`/runs/${id}/analyses`, {
      offset,
      limit: 20,
    }),
  suggestions: (id: string, offset = 0) =>
    get<Schema<"SuggestionResponse">[]>(`/runs/${id}/suggestions`, {
      offset,
      limit: 20,
    }),
  agentTask: (runId: string, name: "analysis" | "suggestion", id: string) =>
    get<Schema<"AnalysisTaskResponse">>(`/runs/${runId}/${name}-tasks/${id}`),
  retryAgent: (runId: string, name: "analysis" | "suggestion", id: string) =>
    post<unknown>(`/runs/${runId}/${name}-tasks/${id}/retry`),
  evidence: (runId: string, id: string) =>
    get<unknown>(`/runs/${runId}/suggestion-tasks/${id}/evidence`),
  mainNetwork: (id: string) =>
    get<Schema<"MainNetworkResponse">>(`/runs/${id}/code/main-network`),
  saveMainNetwork: (id: string, path: string) =>
    put<Schema<"MainNetworkResponse">>(`/runs/${id}/code/main-network`, {
      path,
    }),
  analysisReadiness: (id: string) =>
    get<Schema<"ManualAnalysisResponse">>(`/runs/${id}/analysis-readiness`),
  startAnalysis: (id: string) =>
    post<Schema<"ManualAnalysisResponse">>(`/runs/${id}/analysis/start`),
  files: (id: string, path = ".", offset = 0) =>
    get<Schema<"DirectoryResponse">>(`/runs/${id}/code/files`, {
      path,
      offset,
      limit: 50,
    }),
  file: (id: string, path?: string, start_line = 1, expected_sha256?: string) =>
    get<Schema<"FileResponse">>(`/runs/${id}/code/file`, {
      path,
      start_line,
      expected_sha256,
      line_count: 60,
    }),
  changes: (id: string, offset = 0) =>
    get<Schema<"ChangeResponse">[]>(`/runs/${id}/code-changes`, {
      offset,
      limit: 20,
    }),
  prepareChange: (id: string, body: Schema<"PrepareChange">) =>
    post<Schema<"ChangeResponse">>(`/runs/${id}/code-changes`, body),
  change: (id: string) => get<Schema<"ChangeResponse">>(`/code-changes/${id}`),
  applyChange: (id: string) =>
    post<Schema<"ChangeResponse">>(`/code-changes/${id}/apply`),
  revisions: (id: string) =>
    get<Schema<"RevisionResponse">[]>(`/code-changes/${id}/training-runs`, {
      limit: 20,
    }),
  prepareRevision: (id: string, body: Schema<"PrepareRevision">) =>
    post<Schema<"RevisionResponse">>(`/code-changes/${id}/training-runs`, body),
  revision: (id: string) =>
    get<Schema<"RevisionResponse">>(`/runs/${id}/revision`),
  startRevision: (id: string) =>
    post<Schema<"OperationResponse">>(`/runs/${id}/revision/start`),
  comparison: (id: string) =>
    get<Schema<"ComparisonResponse">>(`/runs/${id}/comparison`),
  references: (id: string) =>
    get<unknown>(`/sessions/${id}/references`, { limit: 50 }),
  advice: (id: string) =>
    get<unknown>(`/sessions/${id}/research-advice`, { limit: 50 }),
  plan: (id: string) => get<unknown>(`/sessions/${id}/training-plan`),
  papers: (offset = 0) =>
    get<Schema<"PaperPage">>("/papers", { offset, limit: 20 }),
  paper: (id: string) => get<Schema<"PaperResponse">>(`/papers/${id}`),
  uploadPaper: async (file: File, title: string, request_id: string) =>
    (
      await http.post<Schema<"PaperResponse">>("/papers", file, {
        params: { title, request_id },
        headers: { "Content-Type": "application/pdf" },
      })
    ).data,
  deletePaper: (id: string) => del(`/papers/${id}`),
  retryPaper: (id: string, rebuild = false) =>
    post<Schema<"PaperResponse">>(`/papers/${id}/retry`, undefined, {
      rebuild,
    }),
  paperFile: async (id: string) =>
    (await http.get<Blob>(`/papers/${id}/file`, { responseType: "blob" })).data,
  chunks: (id: string, offset = 0) =>
    get<Schema<"ChunkPage">>(`/papers/${id}/chunks`, { offset, limit: 20 }),
  chunk: (id: string, offset = 0) =>
    get<Schema<"ChunkResponse">>(`/papers/chunks/${id}`, {
      offset,
      max_bytes: 12000,
    }),
  searchPaper: (query: string, paper_id?: string) =>
    post<Schema<"PaperSearchResponse">>("/papers/search", {
      query,
      ...(paper_id ? { paper_id } : {}),
      limit: 5,
    }),
};
