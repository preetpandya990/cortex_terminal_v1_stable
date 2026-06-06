import { Suspense } from "react";
import HawkEyeRadarClient from "./HawkEyeRadarClient";

export default function HawkEyeRadarPage() {
  return (
    <Suspense>
      <HawkEyeRadarClient />
    </Suspense>
  );
}
