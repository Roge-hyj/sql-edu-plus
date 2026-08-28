import { request } from "@/utils/request";
import type {
  AiLanguage,
  ChatMessage,
  SqlCheckResponse,
  SqlHintResponse,
  SubmissionOut,
  TeachingFeedbackStatus,
  TeachingSupport,
  TeachingSupportGenerationSource,
  TeachingSupportLevel,
  TeachingSupportStatus,
} from "@/types";

// 重新导出类型，保持向后兼容
export type {
  AiLanguage,
  ChatMessage,
  SqlCheckResponse,
  SqlHintResponse,
  SubmissionOut,
  TeachingFeedbackStatus,
  TeachingSupport,
  TeachingSupportGenerationSource,
  TeachingSupportLevel,
  TeachingSupportStatus,
} from "@/types";

/** Create one id for one submit-button action; transport retries reuse it. */
export function createSqlAttemptId(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") {
    return cryptoApi.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === "function") {
    cryptoApi.getRandomValues(bytes);
  } else {
    // UUID uniqueness, not authentication, is the requirement here. This
    // fallback supports constrained mini-app runtimes without Web Crypto.
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex
    .slice(6, 8)
    .join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

export function sqlHint(data: { sql: string }) {
  return request<SqlHintResponse>({
    url: "/ai/sql-hint",
    method: "POST",
    data,
  });
}

export type SqlCheckAttempt = {
  student_sql: string;
  question_id: number;
  /** Reuse only when retrying the same button action and identical payload. */
  attempt_id: string;
  language?: AiLanguage;
};

/** Build and retain this object at the submit-button boundary until terminal response. */
export function createSqlCheckAttempt(
  data: Omit<SqlCheckAttempt, "attempt_id">,
): SqlCheckAttempt {
  return {
    ...data,
    attempt_id: createSqlAttemptId(),
  };
}

export function checkSql(data: SqlCheckAttempt) {
  return request<SqlCheckResponse>({
    url: "/ai/check-sql",
    method: "POST",
    data,
  });
}

export function getMySubmissions(params?: { question_id?: number; limit?: number }) {
  const limit = params?.limit ?? 100;
  const q = typeof params?.question_id === "number" ? `&question_id=${params.question_id}` : "";
  return request<SubmissionOut[]>({
    url: `/ai/submissions?limit=${limit}${q}`,
    method: "GET",
  });
}

export function getSubmission(submissionId: number) {
  return request<SubmissionOut>({
    url: `/ai/submissions/${submissionId}`,
    method: "GET",
  });
}

export function getChatMessages(params: { question_id: number; limit?: number }) {
  const limit = params.limit ?? 80;
  return request<ChatMessage[]>({
    url: `/ai/chat/messages?question_id=${params.question_id}&limit=${limit}`,
    method: "GET",
  });
}

export function clearChatMessages(questionId: number) {
  return request<{ deleted: number }>({
    url: `/ai/chat/messages?question_id=${questionId}`,
    method: "DELETE",
  });
}

export function chatWithTeacher(data: {
  question_id: number;
  message: string;
  language?: AiLanguage;
}) {
  return request<{ reply: string }>({
    url: "/ai/chat",
    method: "POST",
    data,
  });
}
