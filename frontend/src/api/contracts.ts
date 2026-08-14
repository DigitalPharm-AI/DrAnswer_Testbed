export type MessageType = "text" | "selection_box" | "input_box";
export type RequestedReturnType = MessageType;

export interface ApiError {
  code: string;
  message: string;
  retryable: boolean;
  details: Record<string, unknown> | null;
}

export type ApiEnvelope<T> =
  | { success: true; data: T; error: null }
  | { success: false; data: null; error: ApiError };

export type SimulationSpeed = 0 | 1 | 5 | 15 | 30 | 60;

export interface SimulationClock {
  current_time: string;
  is_running: boolean;
  speed_multiplier: SimulationSpeed;
}

export type MedicationStatus = "scheduled" | "taken" | "missed";

export interface MedicationDose {
  dose_event_id: string;
  medication_name: string;
  treatment_area: string;
  slot_label: string;
  scheduled_for: string;
  status: MedicationStatus;
  taken_at: string | null;
}

export interface ActiveScenario {
  scenario_id: string;
  name: string;
  schedule_date: string;
}

export interface NutritionMetric {
  name: string;
  intake: number;
  threshold: number;
  unit: string;
  remaining: number;
  percent: number;
  bar_percent: number;
  daily_exceeded: boolean;
  meal_exceeded: boolean;
  is_warning: boolean;
  basis_label: string;
  risk_rank: number;
}

export interface NutritionMealFood {
  food_ref_id: string;
  food_name: string;
  portion: string;
  nutrients: Record<string, { value: number; unit: string }>;
}

export interface NutritionMeal {
  meal_type: string;
  meal_label: string;
  meal_date: string;
  meal_time: string;
  scenario_key: string | null;
  description: string;
  foods: NutritionMealFood[];
}

export interface NutritionDashboard {
  profile: Record<string, unknown>;
  summary: Record<string, unknown>;
  preferences: Record<string, unknown>;
  metrics: NutritionMetric[];
  meals: NutritionMeal[];
  scenarios: Array<{
    key: string;
    label: string;
    description: string;
    result_label: string;
    is_recorded: boolean;
  }>;
}

export interface UiPolicies {
  medication_schedule_alert: boolean;
  missed_dose_conversation: boolean;
}

export type PolicyKey = keyof UiPolicies;

export interface UiNotification {
  id: string;
  notification_type: string;
  title: string;
  body: string;
  visible_at: string;
  visible_at_label: string;
  acknowledged: boolean;
  metadata: {
    severity: "info" | "reminder" | "warning" | "critical";
  };
  interaction: {
    kind: "open_chat";
    state: "pending" | "ready" | "failed";
    message_id: string | null;
  } | null;
  dose_status: string | null;
  related_dose_event_id: string | null;
}

export interface UiNotificationListData {
  notifications: UiNotification[];
  last_seen_id: string | null;
  current_time: string;
}

export interface UiFeatureFlags {
  medication_side_effect_enabled: boolean;
}

export interface DashboardData {
  features: UiFeatureFlags;
  clock: SimulationClock;
  simulation_ready: boolean;
  active_scenario: ActiveScenario | null;
  medications: MedicationDose[];
  nutrition: NutritionDashboard;
  policies: UiPolicies;
  notifications: UiNotification[];
}

export type BackendServerStatus =
  | "ready"
  | "not_ready"
  | "degraded"
  | "failed"
  | "timeout"
  | "incompatible";
export type AiServerStatus =
  | "ready"
  | "not_ready"
  | "failed"
  | "timeout"
  | "unreachable"
  | "incompatible";

export interface ServiceStatus<TStatus extends string> {
  status: TStatus;
  checked_at: string;
  evidence: string[];
}

export interface SystemStatusData {
  backend_server: ServiceStatus<BackendServerStatus>;
  ai_server: ServiceStatus<AiServerStatus>;
}

export interface TestbedResetData {
  request_id: string;
  reset_applied: true;
  reset_at: string;
}

export interface MedicationScenarioItem {
  medication_id: string;
  medication_name: string;
  dosage: string;
  treatment_area: string;
  slot_label: string;
  scheduled_time: string;
}

export interface MedicationScenario {
  scenario_id: string;
  name: string;
  medications: MedicationScenarioItem[];
}

export interface ScenarioListData {
  scenarios: MedicationScenario[];
}

export interface ScenarioApplyData {
  scenario: ActiveScenario;
  medications: MedicationDose[];
}

export interface ChatTableRow {
  column: string;
  value: string;
}

export interface ChatTable {
  table_title: string | null;
  rows: ChatTableRow[];
}

export interface ChatInputOptions {
  unit: string | null;
  lower: number | null;
  upper: number | null;
  selections: string[] | null;
}

export interface ChatInput {
  type: "number" | "dropdown";
  label: string;
  value: string | number | null;
  options: ChatInputOptions;
}

export interface ChatMessageContent {
  message_title: string | null;
  text: string | null;
  tables: ChatTable[] | null;
  selections: string[] | null;
  inputs: ChatInput[] | null;
}

export interface ChatSyncRequest {
  message: string;
  requested_return_type: RequestedReturnType;
  request_id?: string | null;
  source_message_id?: string | null;
}

export interface ChatSyncData {
  request_id: string;
  user_message_id: string;
  user_sort_sequence: number;
  assistant_message_id: string;
  assistant_sort_sequence: number;
  message_type: MessageType;
  message: ChatMessageContent;
  message_at: string;
  display_message_at: string;
}

export type ChatStreamEvent =
  | {
      type: "start";
      request_id: string;
      status: "processing";
    }
  | {
      type: "text_delta";
      request_id: string;
      sequence: number;
      text: string;
    }
  | {
      type: "completed";
      data: ChatSyncData;
    }
  | {
      type: "error";
      request_id: string;
      error: ApiError;
    };

export interface ChatHistoryMessage {
  message_id: string;
  sort_sequence: number;
  role: string;
  source_message_id: string | null;
  response_message_id: string | null;
  message_type: MessageType | null;
  message: string | null;
  content: ChatMessageContent | null;
  created_at: string;
  processing_status: string;
  opinion_submitted: boolean;
  opinion_submitted_at: string | null;
  reaction: FeedbackReaction | null;
}

export interface ClientResponseTiming {
  status: "running" | "completed" | "failed";
  startedAtMonotonicMs: number;
  firstResponseMs: number | null;
  totalResponseMs: number | null;
}

export interface ClientChatHistoryMessage
  extends Omit<ChatHistoryMessage, "sort_sequence"> {
  sort_sequence: number | null;
  client_request_id?: string;
  delivery_status?: "sending" | "failed";
  delivery_error?: string | null;
  delivery_retryable?: boolean;
  response_timing?: ClientResponseTiming;
}

export interface ChatHistoryDay {
  date: string;
  messages: ChatHistoryMessage[];
}

export interface ClientChatHistoryDay {
  date: string;
  messages: ClientChatHistoryMessage[];
}

export interface ChatHistoryData {
  days: ChatHistoryDay[];
  next_before_date: string | null;
}

interface FeedbackRequestBase {
  request_id: string;
  assistant_message_id: string;
  feedback_at: string;
}

export type FeedbackRequest = FeedbackRequestBase &
  (
    | {
        opinion_text: string;
        reaction?: never;
      }
    | {
        reaction: FeedbackReaction;
        opinion_text?: never;
      }
  );

export interface FeedbackData {
  status: "accepted";
  request_id: string;
  assistant_message_id: string;
  reaction: FeedbackReaction | null;
  opinion_submitted: boolean;
  opinion_submitted_at: string | null;
}

export type FeedbackReaction = "like" | "dislike";
