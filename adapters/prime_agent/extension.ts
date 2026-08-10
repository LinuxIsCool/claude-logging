/** Legion logging wrapper for Prime Agent. */
import { installPiFamilyLogging } from "../pi_family/extension_core.ts";

export default function legionLogging(prime: any) {
  installPiFamilyLogging(prime, { runtime: "prime-agent", captureSource: "prime-agent-extension" });
}
