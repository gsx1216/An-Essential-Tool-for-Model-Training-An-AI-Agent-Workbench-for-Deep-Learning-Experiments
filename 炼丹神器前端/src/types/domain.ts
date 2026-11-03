import type { components } from "./api-schema";
export type Schema<N extends keyof components["schemas"]> =
  components["schemas"][N];
export type User = Schema<"UserResponse">;
export type Session = Schema<"SessionResponse">;
export type Message = Schema<"HistoryMessageResponse">;
export type Run = Schema<"RunResponse">;
export type RunItem = Schema<"RunListItem">;
export type Server = Schema<"ServerResponse">;
export type Experiment = Schema<"ExperimentResponse">;
export type Skill = Schema<"SkillSummary">;
export type ParameterRule = {
  type: "int" | "float" | "str" | "bool";
  required?: boolean;
  min?: number;
  max?: number;
  max_length?: number;
};
export type Parameters = Record<string, string | number | boolean>;
export interface Script {
  script_id: string;
  parameters: Record<string, ParameterRule>;
  max_runtime_seconds: number;
}
export interface TrainingTask {
  task_id: string;
  name: string;
  experiment_id: string;
  server_id: string;
  script_id: string;
  default_parameters?: Parameters;
  parameter_rules?: Record<string, ParameterRule>;
  project?: Schema<"ProjectLaunch"> | null;
}
export interface MetricEvent {
  source_offset: number;
  step: number;
  epoch?: number;
  split: string;
  dataset: string;
  values: Record<string, number>;
}
export interface MetricSummary {
  dataset: string;
  split: string;
  metric_name: string;
  first_value: number;
  last_value: number;
  min_value: number;
  max_value: number;
  count?: number;
}
export interface ToolCall {
  id: number;
  turn_id: string;
  tool_name: string;
  outcome: string;
  arguments: unknown;
  result_metadata: unknown;
  duration_ms: number;
  created_at: string;
}
export interface ToolPage {
  active_run_id: string | null;
  items: ToolCall[];
  next_after_id: number | null;
}
export const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
export const itemsOf = (value: unknown): unknown[] =>
  Array.isArray(value)
    ? value
    : Array.isArray(record(value).items)
      ? (record(value).items as unknown[])
      : [];
