/** Legion logging wrapper for Oh My Pi. */
import { installPiFamilyLogging } from "../pi_family/extension_core.ts";

export default function legionLogging(omp: any) {
  installPiFamilyLogging(omp, { runtime: "omp", captureSource: "omp-extension" });
}
