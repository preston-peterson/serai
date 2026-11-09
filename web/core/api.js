// fetch wrappers. Every call returns parsed JSON or throws ApiError; a 401
// raises the "serai:auth" event so the login cover can take over without each
// caller knowing about auth.

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export async function api(path, { method = "GET", body, signal } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      signal,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError(0, "serai is not answering -- is it still running?");
  }
  const data = await res.json().catch(() => null);
  if (res.status === 401) window.dispatchEvent(new CustomEvent("serai:auth"));
  if (!res.ok) {
    throw new ApiError(res.status, (data && data.error) || `${method} ${path} failed (${res.status})`, data);
  }
  const ver = res.headers.get("X-Serai-Version");
  if (ver) window.dispatchEvent(new CustomEvent("serai:version", { detail: ver }));
  return data;
}

export const get = (path, opts) => api(path, opts);
export const post = (path, body) => api(path, { method: "POST", body: body ?? {} });
export const put = (path, body) => api(path, { method: "PUT", body: body ?? {} });

/** The one session identity used everywhere (maps, data-keys, comparisons). */
export const key = (host, name) => `${host}\u001f${name}`;
export const keyOf = (s) => key(s.host, s.name);
