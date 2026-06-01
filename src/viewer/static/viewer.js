(() => {
  const lightbox = document.getElementById("lightbox");
  const imageNode = document.getElementById("lightbox-image");
  const videoNode = document.getElementById("lightbox-video");
  const captionNode = document.getElementById("lightbox-caption");
  const interactiveSelector = "a, button, input, select, textarea, summary, video, audio";

  if (!lightbox || !imageNode || !videoNode || !captionNode) {
    document.addEventListener("click", (event) => {
      const card = event.target.closest(".js-card-link");
      if (!card || event.target.closest(interactiveSelector)) {
        return;
      }
      const href = card.dataset.href;
      if (href) {
        window.location.href = href;
      }
    });
    return;
  }

  const closeLightbox = () => {
    lightbox.hidden = true;
    imageNode.hidden = true;
    imageNode.removeAttribute("src");
    videoNode.hidden = true;
    videoNode.pause();
    videoNode.removeAttribute("src");
    captionNode.textContent = "";
  };

  document.addEventListener("click", (event) => {
    const card = event.target.closest(".js-card-link");
    if (card && !event.target.closest(interactiveSelector)) {
      const href = card.dataset.href;
      if (href) {
        window.location.href = href;
        return;
      }
    }

    const trigger = event.target.closest(".js-lightbox");
    if (trigger) {
      const src = trigger.dataset.src;
      const kind = trigger.dataset.kind;
      const caption = trigger.dataset.caption || "";
      if (!src) {
        return;
      }
      lightbox.hidden = false;
      captionNode.textContent = caption;
      if (kind === "video") {
        videoNode.src = src;
        videoNode.hidden = false;
        imageNode.hidden = true;
      } else {
        imageNode.src = src;
        imageNode.hidden = false;
        videoNode.hidden = true;
      }
      return;
    }

    if (event.target.closest("[data-lightbox-close]")) {
      closeLightbox();
    }
  });

  document.addEventListener("keydown", (event) => {
    const activeCard = document.activeElement?.closest?.(".js-card-link");
    if (activeCard && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      const href = activeCard.dataset.href;
      if (href) {
        window.location.href = href;
        return;
      }
    }
    if (event.key === "Escape" && !lightbox.hidden) {
      closeLightbox();
    }
  });
})();
