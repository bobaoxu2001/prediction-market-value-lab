import Image from "next/image";
import Link from "next/link";

/**
 * Presentational primitives for the public site.
 *
 * They deliberately reuse the research terminal's tokens and shapes - 3px radii,
 * rule-separated bands, serif headings, monospace figures - rather than
 * introducing a second, softer visual language for the marketing pages. A
 * landing page that looks nothing like the product it sells is a promise the
 * product then has to break.
 */

export function Section({
  id,
  children,
  className = "",
}: {
  id?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section id={id} className={`border-t border-line ${className}`}>
      <div className="mx-auto max-w-6xl px-4 py-14 sm:py-20">{children}</div>
    </section>
  );
}

export function SectionHeading({
  eyebrow,
  title,
  lead,
}: {
  eyebrow?: string;
  title: string;
  lead?: string;
}) {
  return (
    <div className="max-w-2xl">
      {eyebrow ? <p className="t-label">{eyebrow}</p> : null}
      <h2 className={`t-page-title ${eyebrow ? "mt-2" : ""}`}>{title}</h2>
      {lead ? <p className="t-prose mt-3">{lead}</p> : null}
    </div>
  );
}

/**
 * A product surface, shown as a real screenshot beside the claim it evidences.
 *
 * `alt` is required and must describe what the screenshot shows, not repeat the
 * heading: a screenshot whose alt text is its own caption tells a screen-reader
 * user nothing they did not already have.
 */
export function ProductShot({
  src,
  alt,
  width,
  height,
  priority = false,
}: {
  src: string;
  alt: string;
  width: number;
  height: number;
  priority?: boolean;
}) {
  return (
    <figure className="overflow-hidden rounded-[3px] border border-line bg-raised">
      <Image
        src={src}
        alt={alt}
        width={width}
        height={height}
        priority={priority}
        sizes="(min-width: 1024px) 560px, 100vw"
        className="h-auto w-full"
      />
    </figure>
  );
}

export function FeatureRow({
  index,
  title,
  body,
  points,
  href,
  linkLabel,
  shot,
  reverse = false,
}: {
  index: number;
  title: string;
  body: string;
  points: readonly string[];
  href: string;
  linkLabel: string;
  shot: React.ReactNode;
  reverse?: boolean;
}) {
  return (
    <div className="grid items-center gap-8 border-t border-line-subtle py-10 lg:grid-cols-2 lg:gap-14">
      <div className={reverse ? "lg:order-2" : undefined}>
        <p className="t-label">
          {String(index).padStart(2, "0")} — Product surface
        </p>
        <h3 className="t-section-title mt-2 text-[1.25rem]">{title}</h3>
        <p className="t-prose mt-3">{body}</p>
        <ul className="mt-4 space-y-2">
          {points.map((point) => (
            <li key={point} className="flex gap-2 text-sm text-ink-muted">
              <span aria-hidden="true" className="mt-[0.45rem] h-px w-3 shrink-0 bg-line-strong" />
              <span>{point}</span>
            </li>
          ))}
        </ul>
        <Link
          href={href}
          className="mt-5 inline-block text-sm underline decoration-line-strong underline-offset-4 hover:decoration-current"
        >
          {linkLabel}
        </Link>
      </div>
      <div className={reverse ? "lg:order-1" : undefined}>{shot}</div>
    </div>
  );
}

/** A figure with its label and the caveat that makes it honest. */
export function ProofFigure({
  label,
  value,
  note,
  tone = "default",
}: {
  label: string;
  value: string;
  note: string;
  tone?: "default" | "muted";
}) {
  // Timestamps are much longer than counts; stepping them down keeps the band
  // on one line instead of reflowing a single cell and breaking the baseline.
  const size = value.length > 13 ? "text-sm" : "text-xl";
  return (
    <div>
      <dt className="t-label">{label}</dt>
      <dd>
        <div
          className={`num font-semibold leading-snug ${size} ${
            tone === "muted" ? "text-ink-muted" : "text-ink"
          }`}
        >
          {value}
        </div>
        <div className="t-meta mt-1">{note}</div>
      </dd>
    </div>
  );
}

export function Faq({ items }: { items: readonly { q: string; a: React.ReactNode }[] }) {
  return (
    <dl className="mt-8 border-t border-line">
      {items.map((item) => (
        <div key={item.q} className="border-b border-line py-5">
          <dt className="t-sub-title">{item.q}</dt>
          <dd className="t-prose mt-2">{item.a}</dd>
        </div>
      ))}
    </dl>
  );
}
