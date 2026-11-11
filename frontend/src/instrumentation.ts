// Runs once when the Next.js server starts. It reports a session secret still at the env
// template's placeholder, or too short, by name only: NEXTAUTH_SECRET signs every session cookie,
// and the template's value is public. Reported rather than refused, like the backend's
// SECRET_KEY check (backend/app/core/secret_hygiene.py), so an upgrade never stops on it.
const MIN_SECRET_LENGTH = 32;

export function register(): void {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const secret = process.env.NEXTAUTH_SECRET ?? process.env.AUTH_SECRET ?? "";
  let problem: string | null = null;
  if (!secret) problem = "is not set";
  else if (secret.trim().toLowerCase().startsWith("replace_me")) problem = "is still the template placeholder";
  else if (secret.length < MIN_SECRET_LENGTH) problem = `is shorter than ${MIN_SECRET_LENGTH} characters`;
  if (problem) {
    console.error(
      `SECURITY: NEXTAUTH_SECRET ${problem}. Generate one with \`openssl rand -base64 32\`, set it in .env and ` +
        "recreate the frontend container; existing sessions end when it changes.",
    );
  }
}
