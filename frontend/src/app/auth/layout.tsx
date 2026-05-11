/**
 * Auth Layout
 *
 * Simple layout for authentication pages (signin, error).
 * The global app shell suppresses sidebar/header/footer for /auth/* routes.
 */

import { ReactNode } from "react";

interface AuthLayoutProps {
  children: ReactNode;
}

export default function AuthLayout({ children }: AuthLayoutProps) {
  return <>{children}</>;
}
