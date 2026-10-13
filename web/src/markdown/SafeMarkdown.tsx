import type { MouseEvent, ReactNode } from "react";
import Markdown, { type Components, type UrlTransform } from "react-markdown";

const LINK_PROTOCOLS: ReadonlySet<string> = new Set(["http:", "https:", "mailto:"]);

/** spec-09 §8: links only for http(s) and mailto; every other URL (javascript:, data:, relative, ...) is dropped. */
export const safeUrl: UrlTransform = (url) => {
  try {
    const u = new URL(url.trim());
    return LINK_PROTOCOLS.has(u.protocol) ? u.href : null;
  } catch {
    return null;
  }
};

function host(href: string): string {
  const u = new URL(href);
  return u.protocol === "mailto:" ? "email" : u.host;
}

function warn(e: MouseEvent<HTMLAnchorElement>): void {
  const href = e.currentTarget.href;
  if (!window.confirm(`This link leaves Noema and opens ${href}. Continue?`)) e.preventDefault();
}

const components: Components = {
  a({ href, children }): ReactNode {
    if (typeof href !== "string" || href === "") return <span className="md-link-blocked">{children}</span>;
    return (
      <a href={href} target="_blank" rel="noopener noreferrer nofollow" referrerPolicy="no-referrer" onClick={warn}>
        {children}
        <span className="md-link-warning" title="External link">
          {" "}
          ↗ {host(href)}
        </span>
      </a>
    );
  },
  img({ alt }): ReactNode {
    return <span className="md-image-blocked">[image not shown{alt ? `: ${alt}` : ""}]</span>;
  },
};

/**
 * Model output and Markdown derivatives (spec-09 §6.8, §8): react-markdown without raw HTML (`skipHtml`), images
 * disabled (never fetched), links restricted and marked as external. No raw-HTML plugin, no SVG, no HTML passthrough.
 */
export function SafeMarkdown({ text }: { text: string }): ReactNode {
  return (
    <div className="markdown">
      <Markdown skipHtml urlTransform={safeUrl} components={components}>
        {text}
      </Markdown>
    </div>
  );
}
