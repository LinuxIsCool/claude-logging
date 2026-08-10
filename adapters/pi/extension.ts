/** Legion logging wrapper for Pi. */
import { installPiFamilyLogging } from "../pi_family/extension_core.ts";

export default function legionLogging(pi: any) {
  installPiFamilyLogging(pi, { runtime: "pi", captureSource: "pi-extension" });
}
