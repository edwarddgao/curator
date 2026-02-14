import { App } from "@modelcontextprotocol/ext-apps";

const imgEl = document.getElementById("img") as HTMLImageElement;

const app = new App({ name: "Art Curator", version: "1.0.0" });

app.ontoolresult = (result) => {
  const artwork = (result as any).structuredContent?.artwork;
  if (artwork?.thumbnail_url) {
    imgEl.src = artwork.image_url || artwork.thumbnail_url;
    imgEl.alt = artwork.title || "";
    imgEl.style.display = "block";
  }
};

app.connect();
