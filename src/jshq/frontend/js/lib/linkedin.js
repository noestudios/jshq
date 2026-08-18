/* LinkedIn people-search URL builders. linkedin_company_ids is a real array,
   so no comma-string parsing. No scraping — these are manual-check links. */

// LinkedIn truncates very long keyword queries, so cap how many title phrases
// get OR-ed into the combined search. A curated handful beats a clipped list.
const MAX_COMBINED_TITLES = 10;

const PEOPLE_SEARCH = "https://www.linkedin.com/search/results/people/";

function quote(s) {
  // Strip embedded quotes, then wrap — LinkedIn treats a quoted phrase as an
  // exact match instead of scattering the words across the profile.
  return `"${s.replace(/"/g, "").trim()}"`;
}

/** Build the currentCompany facet value, e.g. ["1480","5273045"]. */
function currentCompanyFacet(ids) {
  return `[${ids.map((id) => `"${id}"`).join(",")}]`;
}

function cleanIds(company) {
  return (company.linkedin_company_ids || []).map((s) => s.trim()).filter(Boolean);
}

/**
 * People-search URL for a single role title at a company.
 *
 * With company IDs we use the `currentCompany` facet, which restricts results
 * to people *currently* employed there. Without them we fall back to the
 * keyword approach (title + company name), which also matches past employees.
 */
export function titleSearchUrl(company, title) {
  const params = new URLSearchParams();
  const ids = cleanIds(company);
  if (ids.length) {
    params.set("currentCompany", currentCompanyFacet(ids));
    params.set("keywords", quote(title));
  } else {
    params.set("keywords", `${quote(title)} ${quote(company.name)}`);
  }
  params.set("origin", "GLOBAL_SEARCH_HEADER");
  return `${PEOPLE_SEARCH}?${params.toString()}`;
}

/**
 * People-search URL covering *all* of a company's tracked titles in one query,
 * OR-ed together as a Boolean keyword expression.
 */
export function combinedSearchUrl(company) {
  const titles = (company.linkedin_title_searches || [])
    .map((t) => t.trim())
    .filter(Boolean)
    .slice(0, MAX_COMBINED_TITLES);
  if (titles.length === 0) return null;

  const boolean = `(${titles.map(quote).join(" OR ")})`;
  const params = new URLSearchParams();
  const ids = cleanIds(company);
  if (ids.length) {
    params.set("currentCompany", currentCompanyFacet(ids));
    params.set("keywords", boolean);
  } else {
    params.set("keywords", `${boolean} ${quote(company.name)}`);
  }
  params.set("origin", "GLOBAL_SEARCH_HEADER");
  return `${PEOPLE_SEARCH}?${params.toString()}`;
}

/** True once the combined "All roles" sweep would be more than one title. */
export function hasMultipleTitles(company) {
  return (company.linkedin_title_searches || []).filter((t) => t.trim()).length > 1;
}

/**
 * Company-search URL to help look up a company's numeric ID: search companies,
 * open the target, click "See all employees" — the resulting people-search URL
 * contains `currentCompany=["<id>"]`.
 */
export function companyLookupUrl(name) {
  const params = new URLSearchParams({
    keywords: name,
    origin: "GLOBAL_SEARCH_HEADER",
  });
  return `https://www.linkedin.com/search/results/companies/?${params.toString()}`;
}
