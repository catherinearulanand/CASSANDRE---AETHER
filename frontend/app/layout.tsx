import type { Metadata } from "next";
import { Montserrat } from "next/font/google";
import "@/styles/globals.css";
import Shell from "@/components/Shell";

const montserrat = Montserrat({ subsets: ["latin"], variable: "--font-montserrat", display: "swap" });
export const metadata: Metadata = {
  title: "AETHER · Cassandré (synthetic data)",
  description: "Regulatory-first derivatives decision support. Synthetic data only.",
};
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en" className={montserrat.variable}><body><Shell>{children}</Shell></body></html>;
}
