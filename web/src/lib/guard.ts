import { NextResponse } from "next/server";
import { currentSessionEmail } from "@/lib/auth";

/**
 * The broker snapshot is credential-protected and this app holds that
 * credential server-side, so every page and route proves a session first.
 */
export async function withSession(): Promise<
  { email: string } | { response: NextResponse }
> {
  const email = await currentSessionEmail();
  if (email) {
    return { email };
  }
  return {
    response: NextResponse.json(
      { error: "the control panel requires a signed-in operator" },
      { status: 401 },
    ),
  };
}
