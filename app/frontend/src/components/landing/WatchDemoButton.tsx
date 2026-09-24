import { DEMO_VIDEO_URL } from "@/lib/features";

import { PlayCircleIcon } from "./icons";

/**
 * "Watch Demo" — the landing page's secondary call to action.
 *
 * There is no recording yet, so there is nothing to play and nothing is faked:
 * with no `videoUrl` the button is drawn exactly as designed but announces
 * itself as unavailable (`aria-disabled`, with the reason in its accessible
 * description) and does nothing when pressed. It stays focusable so a keyboard
 * or screen-reader user can still discover it and hear why.
 *
 * Connecting the real video is a one-line change to `DEMO_VIDEO_URL` in
 * `lib/features.ts`; the button then becomes a link to it, opened in a new tab.
 */
export function WatchDemoButton({
  className,
  videoUrl = DEMO_VIDEO_URL,
}: {
  className?: string;
  videoUrl?: string | null;
}) {
  const content = (
    <>
      <PlayCircleIcon />
      <span>Watch Demo</span>
    </>
  );

  if (videoUrl) {
    return (
      <a className={className} href={videoUrl} target="_blank" rel="noopener noreferrer">
        {content}
        <span className="visually-hidden"> (opens in a new tab)</span>
      </a>
    );
  }

  // The reason sits outside the button so it is the button's description,
  // not part of its name: the control is still called "Watch Demo".
  return (
    <>
      <button
        type="button"
        className={className}
        aria-disabled="true"
        aria-describedby="watch-demo-unavailable"
        data-state="unavailable"
        onClick={(event) => event.preventDefault()}
      >
        {content}
      </button>
      <span id="watch-demo-unavailable" className="visually-hidden">
        The demo video is not available yet.
      </span>
    </>
  );
}
