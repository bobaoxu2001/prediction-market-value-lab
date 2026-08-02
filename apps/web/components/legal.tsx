import Link from "next/link";

import { SUPPORT_EMAIL } from "@/lib/site";

/**
 * Shared furniture for the legal pages.
 *
 * The review banner is not decoration. These documents were drafted by an
 * engineer to describe what the software actually does; they have not been read
 * by a lawyer, and the pages say so at the top rather than in a footnote.
 * Removing the banner is a decision for the owner and their counsel, and it
 * should require deleting a line of code that is hard to miss.
 */

export function LegalPage({
  title,
  updated,
  summary,
  children,
}: {
  title: string;
  updated: string;
  summary: string;
  children: React.ReactNode;
}) {
  return (
    <article className="mx-auto max-w-3xl px-4 pb-16 pt-14">
      <p className="t-label">Legal</p>
      <h1 className="t-page-title mt-2">{title}</h1>
      <p className="t-meta mt-2">Last updated {updated}</p>
      <p className="t-prose mt-4">{summary}</p>

      <ReviewNotice />

      <div className="mt-10 space-y-8">{children}</div>

      <p className="mt-12 border-t border-line pt-6 t-meta">
        Questions about this document go to{" "}
        <a
          href={`mailto:${SUPPORT_EMAIL}`}
          className="underline underline-offset-2"
        >
          {SUPPORT_EMAIL}
        </a>
        . See also the{" "}
        <Link href="/terms" className="underline underline-offset-2">
          terms
        </Link>
        , the{" "}
        <Link href="/privacy" className="underline underline-offset-2">
          privacy notice
        </Link>{" "}
        and the{" "}
        <Link href="/risk-disclosure" className="underline underline-offset-2">
          risk disclosure
        </Link>
        .
      </p>
    </article>
  );
}

export function ReviewNotice() {
  return (
    <aside
      className="mt-6 rounded-[3px] border border-warn/50 border-l-2 border-l-warn bg-warn/10 px-4 py-3 text-sm"
      aria-label="Review status"
    >
      <p className="t-label text-warn">Draft — not reviewed by counsel</p>
      <p className="mt-1.5 text-ink">
        This document is a foundation written to describe the software&apos;s
        actual behaviour. It has <strong>not</strong> been reviewed or approved
        by a lawyer, and it contains bracketed placeholders where an owner or
        legal decision is required. It must not be relied on as a finished legal
        agreement, and billing must not be activated until the placeholders are
        resolved and the document has been reviewed.
      </p>
    </aside>
  );
}

export function LegalSection({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="border-t border-line-subtle pt-6">
      <h2 className="t-section-title">{title}</h2>
      <div className="mt-3 space-y-3 text-sm leading-relaxed text-ink-muted">
        {children}
      </div>
    </section>
  );
}

/** A value the owner must supply. Rendered so it is impossible to overlook. */
export function Placeholder({ children }: { children: React.ReactNode }) {
  return (
    <mark className="rounded-[2px] bg-warn/20 px-1 font-mono text-[0.8125rem] text-ink">
      [{children} — OWNER INPUT REQUIRED]
    </mark>
  );
}
