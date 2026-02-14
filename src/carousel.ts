import { App } from "@modelcontextprotocol/ext-apps";

interface Artwork {
  id: number;
  met_object_id: number;
  title: string;
  artist_name: string | null;
  artist_bio: string | null;
  artist_nationality: string | null;
  artist_birth_year: number | null;
  artist_death_year: number | null;
  object_date: string | null;
  date_begin: number | null;
  date_end: number | null;
  medium: string | null;
  dimensions: string | null;
  department: string | null;
  classification: string | null;
  culture: string | null;
  period: string | null;
  caption: string | null;
  keywords: string | null;
  image_url: string | null;
  thumbnail_url: string;
  object_url: string | null;
  credit_line: string | null;
  accession_number: string | null;
  gallery_number: string | null;
  is_highlight: number;
}

function escapeHtml(str: string): string {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

const loadingEl = document.getElementById("loading")!;
const carouselEl = document.getElementById("carousel")!;

let selectedId: number | null = null;

function renderCarousel(artworks: Artwork[]): void {
  loadingEl.style.display = "none";
  carouselEl.innerHTML = "";

  for (const art of artworks) {
    const card = document.createElement("div");
    card.className = "card";
    card.dataset.id = String(art.id);

    const artistLine = art.artist_name
      ? escapeHtml(art.artist_name)
      : "";
    const dateLine = art.object_date
      ? escapeHtml(art.object_date)
      : "";

    card.innerHTML = `
      <img
        class="card-img"
        src="${escapeHtml(art.thumbnail_url)}"
        alt="${escapeHtml(art.title)}"
        loading="lazy"
      />
      <div class="card-info">
        <div class="card-title">${escapeHtml(art.title)}</div>
        ${artistLine ? `<div class="card-artist">${artistLine}</div>` : ""}
        ${dateLine ? `<div class="card-date">${dateLine}</div>` : ""}
      </div>
    `;

    card.addEventListener("click", () => onCardClick(art, card));
    carouselEl.appendChild(card);
  }
}

function onCardClick(art: Artwork, card: HTMLElement): void {
  // Update selection UI
  const prev = carouselEl.querySelector(".card.selected");
  if (prev) prev.classList.remove("selected");
  card.classList.add("selected");
  selectedId = art.id;

  // Push full metadata to Claude
  app.updateModelContext({
    structuredContent: {
      type: "artwork_selected",
      data: {
        id: art.id,
        title: art.title,
        artist_name: art.artist_name,
        artist_bio: art.artist_bio,
        object_date: art.object_date,
        medium: art.medium,
        department: art.department,
        classification: art.classification,
        culture: art.culture,
        period: art.period,
        caption: art.caption,
        image_url: art.image_url,
        object_url: art.object_url,
        credit_line: art.credit_line,
        gallery_number: art.gallery_number,
      },
    },
  });
}

// --- MCP App setup ---
const app = new App({ name: "Art Curator", version: "1.0.0" });

app.ontoolresult = (result) => {
  const artworks = (result as any).structuredContent?.artworks as
    | Artwork[]
    | undefined;
  if (artworks && artworks.length > 0) {
    selectedId = null;
    renderCarousel(artworks);
  }
};

app.connect();
