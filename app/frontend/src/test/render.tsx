import { render } from "@testing-library/react";

import { AppFrame } from "@/app/AppFrame";
import { AppProviders } from "@/app/providers";

/**
 * Mount a page inside the real application frame.
 *
 * Not a bare `render(<Page />)`. The session gate, the shell, the navigation
 * and the shared conversation state all live above the page in the layout, and
 * a page rendered without them exercises none of it — which is how a chat that
 * silently dropped every message under real authentication passed a hundred
 * tests. What a test drives here is what a browser loads.
 */
export function renderApp(ui: React.ReactNode) {
  return render(
    <AppProviders>
      <AppFrame>{ui}</AppFrame>
    </AppProviders>,
  );
}
