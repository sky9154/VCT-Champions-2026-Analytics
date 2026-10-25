import type { ApiResponse } from "./types";


const USER_MESSAGES: Record<string, string> = {
  INVALID_PARAMETER: "查詢條件有誤，請調整後重試。",
  INVALID_FILTER: "篩選條件不支援，請調整後重試。",
  NOT_FOUND: "找不到要求的資料。",
  DATABASE_ERROR: "目前無法取得資料，請稍後再試。",
  DATA_UNAVAILABLE: "目前無法取得資料，請稍後再試。",
  INTERNAL_ERROR: "目前無法取得資料，請稍後再試。"
};

export class ApiError extends Error {
  readonly code: string;
  readonly userMessage: string;
  readonly statusCode: number | null;

  constructor(code: string, userMessage: string, statusCode: number | null = null) {
    super(userMessage);
    this.name = "ApiError";
    this.code = code;
    this.userMessage = userMessage;
    this.statusCode = statusCode;
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

export const toApiError = (error: unknown): ApiError => {
  if (error instanceof ApiError) {
    return error;
  }

  return new ApiError("REQUEST_FAILED", "目前無法取得資料，請稍後再試。");
};

export const isAbortError = (error: unknown): boolean => (
  error instanceof DOMException && error.name === "AbortError"
);

export const getServiceErrorMessage = (error: unknown): string | null => {
  if (!(error instanceof ApiError)) {
    return null;
  }

  if (error.code === "NETWORK_ERROR" || error.code === "INVALID_RESPONSE") {
    return error.userMessage;
  }

  if (error.statusCode === 404 || error.statusCode === 405) {
    return "API 服務版本不相容，請更新並重新啟動服務。";
  }

  return null;
};

const request = async <T>(
  path: string,
  method: "GET" | "POST" | "PATCH",
  signal?: AbortSignal,
  body?: unknown
): Promise<ApiResponse<T>> => {
  let response: Response;

  try {
    const headers: Record<string, string> = { Accept: "application/json" };

    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }

    response = await fetch(`/api${path.startsWith("/") ? path : `/${path}`}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal
    });
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }

    throw new ApiError("NETWORK_ERROR", "目前無法連線至 API 服務，請確認服務已啟動。");
  }

  let payload: unknown;

  try {
    payload = await response.json();
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }

    throw new ApiError("INVALID_RESPONSE", "API 服務版本不相容，請更新並重新啟動服務。", response.status);
  }

  if (!response.ok) {
    const envelope = isRecord(payload) && isRecord(payload.error) ? payload.error : null;
    const code = envelope && typeof envelope.code === "string" ? envelope.code : "REQUEST_FAILED";

    throw new ApiError(code, USER_MESSAGES[code] ?? "目前無法完成請求，請稍後重試。", response.status);
  }

  if (!isRecord(payload) || !("data" in payload) || !isRecord(payload.meta)) {
    throw new ApiError("INVALID_RESPONSE", "API 服務版本不相容，請更新並重新啟動服務。", response.status);
  }

  return payload as unknown as ApiResponse<T>;
};

export const requestJson = <T>(path: string, signal?: AbortSignal): Promise<ApiResponse<T>> => (
  request<T>(path, "GET", signal)
);

export const requestPostJson = <T>(path: string, signal?: AbortSignal): Promise<ApiResponse<T>> => (
  request<T>(path, "POST", signal)
);

export const requestPatchJson = <T>(path: string, body: unknown, signal?: AbortSignal): Promise<ApiResponse<T>> => (
  request<T>(path, "PATCH", signal, body)
);
