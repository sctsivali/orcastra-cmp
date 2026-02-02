/**
 * Auth Layout
 *
 * Simple layout for authentication pages (signin, error)
 * that doesn't include the main navigation.
 */

import { ReactNode } from "react";

interface AuthLayoutProps {
  children: ReactNode;
}

export default function AuthLayout({ children }: AuthLayoutProps) {
  return <>{children}</>;
}
