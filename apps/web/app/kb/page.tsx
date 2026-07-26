import type { Metadata } from "next";

import { UnderConstruction } from "@/components/under-construction";

export const metadata: Metadata = { title: "知识库" };

export default function Page() {
  return <UnderConstruction title="知识库" />;
}
