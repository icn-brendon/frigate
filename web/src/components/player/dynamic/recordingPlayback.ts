import type { StreamQuality } from "@/types/record";

type RecordingPlayback<T> = {
  fallbackQuality?: StreamQuality;
  quality: StreamQuality;
  recordings?: T[];
};

export function resolveRecordingPlayback<T>(
  requestedQuality: StreamQuality,
  requestedRecordings?: T[],
  fallbackRecordings?: T[],
): RecordingPlayback<T> {
  if (requestedRecordings === undefined || requestedRecordings.length > 0) {
    return {
      quality: requestedQuality,
      recordings: requestedRecordings,
    };
  }

  const fallbackQuality = requestedQuality === "sub" ? "main" : "sub";

  return {
    fallbackQuality,
    quality: fallbackQuality,
    recordings: fallbackRecordings,
  };
}
