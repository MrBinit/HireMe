import "./globals.css";
import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "HireMe Frontend",
  description: "Candidate apply and admin hiring dashboard",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>
        <header className="site-header">
          <div className="site-header-inner">
            <Link href="/" className="brand">
              HireMe
            </Link>
            <nav className="site-nav" aria-label="Primary">
              <Link href="/">Apply</Link>
              <Link href="/admin">Admin</Link>
              <Link href="/referee">Referee</Link>
            </nav>
          </div>
        </header>
        <div className="app-frame">{children}</div>
      </body>
    </html>
  );
}
