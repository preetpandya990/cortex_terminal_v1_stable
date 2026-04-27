import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";
import Link from "next/link";
import { AuthStatus } from "@/components/auth/AuthStatus";
import { DevLoginButton } from "@/components/auth/DevLoginButton";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Cortex Terminal V1",
  description: "AI/ML-powered trading platform for Indian stock markets",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <Providers>
          <div className="min-h-screen bg-[radial-gradient(1200px_400px_at_top,_#f1f5f9,_transparent)]">
            <header className="border-b border-slate-200/80 bg-white/80 backdrop-blur">
              <div className="mx-auto flex w-full max-w-7xl items-center justify-between px-4 py-4">
                <Link href="/" className="text-lg font-semibold tracking-tight">
                  Cortex Terminal V1
                </Link>
                <nav className="flex items-center gap-6 text-sm font-medium text-slate-600">
                  <Link href="/" className="transition-colors hover:text-slate-900">
                    Dashboard
                  </Link>
                  <Link href="/hawk-eye-radar" className="transition-colors hover:text-slate-900">
                    Hawk-Eye-Radar
                  </Link>
                  <Link href="/scanner" className="transition-colors hover:text-slate-900">
                    Scanner
                  </Link>
                  <Link href="/cortex-ai" className="transition-colors hover:text-slate-900">
                    Cortex AI
                  </Link>
                </nav>
                <div className="flex items-center gap-4">
                  <AuthStatus />
                  <DevLoginButton />
                </div>
              </div>
            </header>
            <main className="mx-auto w-full max-w-7xl px-4 py-10">
              {children}
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
