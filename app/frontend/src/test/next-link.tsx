/**
 * A test double for `next/link`.
 *
 * The real component needs the App Router's context, which does not exist
 * under jsdom. This renders the anchor it would render and drives the test
 * router on click, so navigation assertions describe what a user's click does
 * rather than what a mock recorded.
 */

import { useRouter } from "./next-navigation";

type Href = string | { pathname: string; query?: Record<string, string> };

function toHref(href: Href): string {
  if (typeof href === "string") return href;
  const search = new URLSearchParams(href.query ?? {}).toString();
  return search ? `${href.pathname}?${search}` : href.pathname;
}

export default function Link({
  href,
  children,
  onClick,
  ...rest
}: {
  href: Href;
  children: React.ReactNode;
} & Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  const router = useRouter();
  const target = toHref(href);

  return (
    <a
      {...rest}
      href={target}
      onClick={(event) => {
        event.preventDefault();
        onClick?.(event);
        router.push(target);
      }}
    >
      {children}
    </a>
  );
}
