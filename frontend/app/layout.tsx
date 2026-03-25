import "./globals.css";
import type { Metadata } from "next";

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
      <body>{children}</body>
    </html>
  );
}
