import type { Metadata } from "next";

import { UnderConstruction } from "@/components/under-construction";

export const metadata: Metadata = { title: "我的画像" };

export default function Page() {
  return <UnderConstruction title="我的画像" />;
}
