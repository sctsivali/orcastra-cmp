"use client";

/**
 * Auth Provider Component
 *
 * Wraps the application with SessionProvider for client-side session access.
 * This enables the useSession() hook throughout the app.
 */

import { SessionProvider } from "next-auth/react";
import { ReactNode } from "react";

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  return <SessionProvider>{children}</SessionProvider>;
}
