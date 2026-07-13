export type HttpRequestOptions = Omit<RequestInit, "headers"> & {
  headers?: HeadersInit;
  token?: string;
};

export class HttpError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.body = body;
  }
}

function withAuthorization(headers: HeadersInit | undefined, token: string | undefined) {
  const nextHeaders = new Headers(headers);
  if (token) nextHeaders.set("Authorization", `Bearer ${token}`);
  return nextHeaders;
}

function errorMessage(body: unknown, fallback: string) {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    for (const key of ["detail", "message", "error"]) {
      if (typeof record[key] === "string" && record[key]) return record[key];
    }
  }
  return typeof body === "string" && body ? body : fallback;
}

async function toHttpError(response: Response) {
  const text = await response.text();
  let body: unknown = text;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // Preserve non-JSON response text for existing status displays.
    }
  }
  return new HttpError(response.status, errorMessage(body, response.statusText || `HTTP ${response.status}`), body);
}

export async function request(input: RequestInfo | URL, options: HttpRequestOptions = {}) {
  const { token, headers, ...init } = options;
  const response = await fetch(input, {
    ...init,
    headers: withAuthorization(headers, token),
  });
  if (!response.ok) throw await toHttpError(response);
  return response;
}

export async function getJson<T>(input: RequestInfo | URL, options: HttpRequestOptions = {}): Promise<T> {
  const response = await request(input, options);
  return response.json() as Promise<T>;
}

export function isAbortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}
