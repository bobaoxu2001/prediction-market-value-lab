// @vitest-environment jsdom
/**
 * The public legal documents must not publish a postal address.
 *
 * PMVL is operated by one individual with no company and no business premises,
 * so the only postal address it has ever had is a private home. It was rendered
 * verbatim on `/terms` (twice) and `/privacy` to satisfy the notices clauses,
 * which put a residence on a public, indexed, permanently-archived site in
 * exchange for a formality that a monitored contact channel already meets.
 *
 * These tests assert the *shape* of the defect rather than the one string that
 * caused it. A future edit that reintroduces a different residence - or the same
 * one written differently - fails here, which a literal-string check would not
 * catch. And the last test guards the opposite failure: deleting the address and
 * leaving a blank where a reader expects a term.
 */

import { cleanup, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import * as seller from "@/lib/seller";

/** The three documents a visitor is asked to accept, plus the seller module. */
const PUBLIC_LEGAL_PAGES = [
  "@/app/(site)/terms/page",
  "@/app/(site)/privacy/page",
  "@/app/(site)/risk-disclosure/page",
  "@/app/(site)/founding-pilot/page",
] as const;

/**
 * A street line: a house number followed by a street type.
 *
 * Deliberately requires the number, so "Washington" as a jurisdiction or
 * "Park Avenue Partners" as a name does not trip it - only something shaped like
 * somewhere mail is delivered.
 */
const STREET_LINE =
  /\b\d{1,6}\s+[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,4}\s+(?:Street|St\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Road|Rd\.?|Drive|Dr\.?|Lane|Ln\.?|Court|Ct\.?|Place|Pl\.?|Terrace|Ter\.?|Parkway|Pkwy\.?|Highway|Hwy\.?|Way|Circle|Cir\.?)\b/;

/** A dwelling subdivision. On its own this is already a residential tell. */
const UNIT_LINE = /\b(?:Apt|Apartment|Unit|Suite|Ste|Flat|#)\.?\s*\d+[A-Za-z]?\b/;

/** `NJ 07310` - a US state code followed by a ZIP. */
const STATE_AND_ZIP =
  /\b(?:A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\s+\d{5}(?:-\d{4})?\b/;

async function renderText(specifier: string): Promise<string> {
  const { default: Page } = await import(specifier);
  const element = specifier.includes("founding-pilot") ? await Page() : <Page />;
  const text = render(element).container.textContent ?? "";
  cleanup();
  return text;
}

describe("the public legal documents publish no postal address", () => {
  it.each(PUBLIC_LEGAL_PAGES)("%s renders no street line", async (specifier) => {
    const text = await renderText(specifier);
    expect(text.match(STREET_LINE)?.[0] ?? null).toBeNull();
  });

  it.each(PUBLIC_LEGAL_PAGES)("%s renders no dwelling unit", async (specifier) => {
    const text = await renderText(specifier);
    expect(text.match(UNIT_LINE)?.[0] ?? null).toBeNull();
  });

  it.each(PUBLIC_LEGAL_PAGES)("%s renders no state-and-ZIP pair", async (specifier) => {
    const text = await renderText(specifier);
    // A bare state name is a jurisdiction and is required; a state code beside a
    // ZIP is the last line of a mailing address and is not.
    expect(text.match(STATE_AND_ZIP)?.[0] ?? null).toBeNull();
  });

  it("exports no constant whose value is shaped like an address", () => {
    const offenders = Object.entries(seller)
      .flatMap(([name, value]) =>
        typeof value === "string" ? [[name, value] as const] : [],
      )
      .filter(
        ([, value]) =>
          UNIT_LINE.test(value) ||
          STATE_AND_ZIP.test(value) ||
          STREET_LINE.test(value),
      )
      .map(([name]) => name);
    expect(offenders).toEqual([]);
  });

  it("still states how a notice reaches the seller", async () => {
    // The opposite failure. A notices clause that silently loses its address is
    // a worse document than one that explains why there is not a postal one.
    expect(seller.LEGAL_NOTICE_METHOD).toMatch(/notice/i);
    expect(seller.LEGAL_NOTICE_METHOD).toMatch(/on request/i);
    expect(await renderText("@/app/(site)/terms/page")).toContain(
      seller.LEGAL_NOTICE_METHOD,
    );
  });
});
