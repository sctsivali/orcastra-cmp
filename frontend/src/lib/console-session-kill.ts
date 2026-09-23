interface SessionKillBody {
  success?: boolean;
  error?: string | null;
  detail?: unknown;
}

const FALLBACK = "The session could not be terminated";

// note: the kill route answers 200 with success false when it did not cancel the session (its
// read of the operation failed, or the operation belongs to another instance), so the HTTP
// status alone cannot say the session is gone. Null means it was terminated or already ended.
export async function sessionKillError(response: Response): Promise<string | null> {
  const body = (await response.json().catch(() => null)) as SessionKillBody | null;
  if (response.ok && body?.success === true) return null;
  if (body?.error) return body.error;
  return typeof body?.detail === "string" ? body.detail : FALLBACK;
}
