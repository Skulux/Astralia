// Tiny sparkle on brand icon for fun ✦
document.addEventListener("DOMContentLoaded", () => {
  const logo = document.querySelector(".brand-logo");
  if (logo) {
    let t = 0;
    const baseGlow = 8;
    const range = 16;
    setInterval(() => {
      t += 0.03;
      const glow = baseGlow + Math.abs(Math.sin(t)) * range;
      logo.style.boxShadow = "none";
      logo.style.filter = `drop-shadow(0 0 ${glow}px rgba(193,170,255,0.85))`;
    }, 50);
  }

  const initMobileNav = () => {
    const navToggle = document.querySelector("[data-nav-toggle]");
    const nav = document.querySelector("[data-nav]");
    if (!navToggle || !nav) {
      return;
    }

    const navBackdrop = document.querySelector("[data-nav-backdrop]");
    const navLinks = Array.from(nav.querySelectorAll("a[href]"));
    const navMediaQuery = window.matchMedia("(min-width: 901px)");
    let previousFocus = null;

    const updateAriaHidden = () => {
      const shouldHide = !navMediaQuery.matches && !document.body.classList.contains("nav-open");
      if (shouldHide) {
        nav.setAttribute("aria-hidden", "true");
      } else {
        nav.removeAttribute("aria-hidden");
      }
    };

    const setOpen = (open, { focusToggle = true } = {}) => {
      const wasOpen = document.body.classList.contains("nav-open");
      document.body.classList.toggle("nav-open", open);
      nav.classList.toggle("is-open", open);
      navToggle.classList.toggle("is-active", open);
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (navBackdrop) {
        navBackdrop.classList.toggle("is-visible", open);
      }
      updateAriaHidden();
      if (open && !wasOpen) {
        previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        const focusTarget = nav.querySelector("a, button, [tabindex='0']");
        if (focusTarget instanceof HTMLElement) {
          focusTarget.focus({ preventScroll: true });
        }
      } else if (!open && wasOpen) {
        const focusTarget =
          previousFocus && previousFocus instanceof HTMLElement ? previousFocus : navToggle;
        previousFocus = null;
        if (focusToggle && focusTarget instanceof HTMLElement) {
          focusTarget.focus({ preventScroll: true });
        }
      }
    };

    const openNav = () => {
      setOpen(true, { focusToggle: false });
    };

    const closeNav = (focusToggle = true) => {
      setOpen(false, { focusToggle });
    };

    navToggle.addEventListener("click", () => {
      const expanded = navToggle.getAttribute("aria-expanded") === "true";
      if (expanded) {
        closeNav(true);
      } else {
        openNav();
      }
    });

    if (navBackdrop) {
      navBackdrop.addEventListener("click", () => {
        closeNav(false);
      });
    }

    navLinks.forEach((link) => {
      link.addEventListener("click", () => {
        if (!navMediaQuery.matches) {
          closeNav(false);
        }
      });
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
        event.preventDefault();
        closeNav(true);
      }
    });

    const handleMediaChange = () => {
      setOpen(false, { focusToggle: false });
      updateAriaHidden();
    };

    if (typeof navMediaQuery.addEventListener === "function") {
      navMediaQuery.addEventListener("change", handleMediaChange);
    } else if (typeof navMediaQuery.addListener === "function") {
      navMediaQuery.addListener(handleMediaChange);
    }

    navToggle.setAttribute("aria-expanded", "false");
    nav.classList.remove("is-open");
    navToggle.classList.remove("is-active");
    if (navBackdrop) {
      navBackdrop.classList.remove("is-visible");
    }
    updateAriaHidden();
  };

  initMobileNav();

  const initAdminFieldsets = () => {
    document.querySelectorAll(".admin-fieldset").forEach((fieldset) => {
      if (fieldset.dataset.collapsibleInitialized === "true") {
        return;
      }
      const legend = fieldset.querySelector("legend");
      if (!legend) {
        return;
      }

      const content = document.createElement("div");
      content.className = "admin-fieldset__content";
      const fragment = document.createDocumentFragment();
      while (legend.nextSibling) {
        fragment.appendChild(legend.nextSibling);
      }

      if (!fragment.childNodes.length) {
        return;
      }

      content.appendChild(fragment);
      fieldset.appendChild(content);

      legend.setAttribute("tabindex", "0");
      legend.setAttribute("role", "button");
      legend.setAttribute("aria-expanded", "false");
      fieldset.dataset.collapsibleInitialized = "true";

      const setOpen = (open) => {
        fieldset.dataset.collapsed = open ? "false" : "true";
        legend.setAttribute("aria-expanded", open ? "true" : "false");
        content.hidden = !open;
      };

      const startOpen =
        fieldset.dataset.defaultOpen === "true" || fieldset.classList.contains("admin-team--new");
      setOpen(startOpen);

      const toggle = () => {
        const shouldOpen = fieldset.dataset.collapsed !== "false";
        setOpen(shouldOpen);
      };

      legend.addEventListener("click", (event) => {
        event.preventDefault();
        toggle();
      });

      legend.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggle();
        }
      });
    });
  };

  initAdminFieldsets();

  const initDynamicArtworksGalleries = () => {
    document.querySelectorAll("[data-artworks-gallery]").forEach((container) => {
      if (container.dataset.artworksInitialized === "true") {
        return;
      }

      const template = container.querySelector("template[data-artworks-new-template]");
      const addButton = container.querySelector("[data-artworks-add]");
      const list = container.querySelector("[data-artworks-new-list]");
      const counter = container.querySelector("[data-artworks-new-count]");

      if (!template || !addButton || !list || !counter) {
        return;
      }

      let nextIndex = parseInt(counter.value || "0", 10);
      if (!Number.isFinite(nextIndex) || nextIndex < 0) {
        nextIndex = 0;
      }

      const applyNameTemplates = (root, index) => {
        root.querySelectorAll("[data-name-template]").forEach((element) => {
          const nameTemplate = element.getAttribute("data-name-template");
          if (!nameTemplate) {
            return;
          }
          const newName = nameTemplate.replace(/__INDEX__/g, String(index));
          element.setAttribute("name", newName);
        });
      };

      const addEntry = () => {
        const clone = template.content.cloneNode(true);
        const fieldset = clone.querySelector(".admin-fieldset");
        if (!fieldset) {
          return;
        }
        applyNameTemplates(fieldset, nextIndex);
        list.appendChild(clone);
        counter.value = String(nextIndex + 1);
        nextIndex += 1;
        initAdminFieldsets();
      };

      addButton.addEventListener("click", (event) => {
        event.preventDefault();
        addEntry();
      });

      container.addEventListener("click", (event) => {
        const target = event.target;
        if (!(target instanceof Element)) {
          return;
        }
        const removeTrigger = target.closest("[data-artworks-remove]");
        if (!removeTrigger) {
          return;
        }
        event.preventDefault();
        const fieldset = removeTrigger.closest(".admin-fieldset");
        if (fieldset) {
          fieldset.remove();
        }
      });

      container.dataset.artworksInitialized = "true";

      if (container.dataset.artworksAutoAdd === "true" && !list.children.length) {
        addEntry();
      }
    });
  };

  initDynamicArtworksGalleries();

  const initRotatingGalleries = () => {
    document.querySelectorAll("[data-rotate-gallery]").forEach((gallery) => {
      const slides = Array.from(gallery.querySelectorAll("[data-gallery-item]"));
      if (!slides.length) {
        return;
      }

      const viewport = gallery.querySelector("[data-artworks-viewport]");
      const dots = Array.from(gallery.querySelectorAll("[data-gallery-dot]"));
      const interval = parseInt(gallery.getAttribute("data-interval"), 10) || 5000;
      let currentIndex = slides.findIndex((slide) => slide.classList.contains("is-active"));
      if (currentIndex < 0) {
        currentIndex = 0;
      }
      let timerId = null;

      const cushionAttribute = parseInt(
        gallery.getAttribute("data-viewport-cushion"),
        10
      );
      const viewportCushion = Number.isFinite(cushionAttribute)
        ? cushionAttribute
        : 120;
      let viewportPeakHeight = viewport ? Math.ceil(viewport.offsetHeight) : 0;

      const syncViewportHeight = () => {
        if (!viewport) {
          return;
        }
        let maxHeight = 0;
        slides.forEach((slide) => {
          const isActive = slide.classList.contains("is-active");
          if (!isActive) {
            slide.classList.add("is-measuring");
          }
          const height = slide.offsetHeight;
          if (height > maxHeight) {
            maxHeight = height;
          }
          if (!isActive) {
            slide.classList.remove("is-measuring");
          }
        });
        if (maxHeight > 0) {
          const rounded = Math.ceil(maxHeight);
          const baselineHeight = Math.max(0, rounded - viewportCushion);
          if (baselineHeight > viewportPeakHeight) {
            viewportPeakHeight = baselineHeight;
          }
          const targetHeight = viewportPeakHeight + viewportCushion;
          viewport.style.minHeight = `${targetHeight}px`;
          viewport.style.height = `${targetHeight}px`;
        }
      };

      const setActive = (nextIndex) => {
        slides.forEach((slide, index) => {
          slide.classList.toggle("is-active", index === nextIndex);
        });
        if (dots.length) {
          dots.forEach((dot, index) => {
            dot.classList.toggle("is-active", index === nextIndex);
          });
        }
        currentIndex = nextIndex;
        window.requestAnimationFrame(syncViewportHeight);
      };

      const stop = () => {
        if (timerId !== null) {
          window.clearInterval(timerId);
          timerId = null;
        }
      };

      const start = () => {
        stop();
        if (slides.length <= 1) {
          return;
        }
        timerId = window.setInterval(() => {
          const next = (currentIndex + 1) % slides.length;
          setActive(next);
        }, interval);
      };

      if (dots.length) {
        dots.forEach((dot, index) => {
          dot.addEventListener("click", () => {
            setActive(index);
            start();
          });
        });
      }

      gallery.addEventListener("mouseenter", stop);
      gallery.addEventListener("mouseleave", start);

      setActive(currentIndex);
      syncViewportHeight();

      if (viewport) {
        const handleResize = () => {
          window.requestAnimationFrame(syncViewportHeight);
        };
        window.addEventListener("resize", handleResize);

        slides.forEach((slide) => {
          slide.querySelectorAll("img").forEach((img) => {
            if (img.complete) {
              return;
            }
            img.addEventListener("load", () => {
              window.requestAnimationFrame(syncViewportHeight);
            });
          });
        });
      }

      start();
    });
  };

  initRotatingGalleries();

  let dragGuardAttached = false;

  const initArtworkGuards = () => {
    document.querySelectorAll("[data-artworks-viewport]").forEach((viewport) => {
      if (viewport.dataset.guardInitialized === "true") {
        return;
      }
      viewport.addEventListener("contextmenu", (event) => {
        event.preventDefault();
      });
      viewport.dataset.guardInitialized = "true";
    });

    document.querySelectorAll("[data-artworks-media]").forEach((media) => {
      if (media.dataset.guardInitialized === "true") {
        return;
      }
      media.setAttribute("draggable", "false");
      media.addEventListener("contextmenu", (event) => {
        event.preventDefault();
      });
      media.addEventListener("touchstart", (event) => {
        if (event.touches && event.touches.length > 1) {
          event.preventDefault();
        }
      }, { passive: false });
      const image = media.querySelector("img");
      if (image) {
        image.setAttribute("draggable", "false");
        image.addEventListener("contextmenu", (event) => {
          event.preventDefault();
        });
      }
      media.dataset.guardInitialized = "true";
    });

    if (!dragGuardAttached) {
      document.addEventListener("dragstart", (event) => {
        const target = event.target;
        if (!(target instanceof Element)) {
          return;
        }
        if (target.closest("[data-artworks-media]")) {
          event.preventDefault();
        }
      });
      dragGuardAttached = true;
    }
  };

  initArtworkGuards();

  const initContentBuilders = () => {
    const sanitizeSplitLayout = (value) => {
      const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
      if (["text-left", "text-right", "image-top", "text-top"].includes(normalized)) {
        return normalized;
      }
      return "text-left";
    };

    const BLOCK_CONFIG = {
      text: {
        label: "Text",
        defaults: () => ({ heading: "", body: "" }),
        build: (container, block, sync) => {
          container.appendChild(
            createLabeledInput("Überschrift", block.heading, (value) => {
              block.heading = value;
              sync();
            })
          );
          container.appendChild(
            createLabeledTextarea("Text", block.body, (value) => {
              block.body = value;
              sync();
            }, { rows: 5 })
          );
        },
      },
      image: {
        label: "Bild",
        defaults: () => ({ heading: "", image: "", alt: "", caption: "" }),
        build: (container, block, sync, context) => {
          container.appendChild(
            createLabeledInput("Überschrift", block.heading, (value) => {
              block.heading = value;
              sync();
            })
          );
          container.appendChild(
            createLabeledInput("Bild (Pfad oder URL)", block.image, (value) => {
              block.image = value;
              sync();
            })
          );
          container.appendChild(
            createLabeledInput("Alt-Text", block.alt, (value) => {
              block.alt = value;
              sync();
            })
          );
          container.appendChild(
            createLabeledTextarea("Bildunterschrift", block.caption, (value) => {
              block.caption = value;
              sync();
            }, { rows: 2 })
          );
          if (context && typeof context.createUploadField === "function") {
            const uploadField = context.createUploadField("Bild hochladen", ["image", "upload"]);
            if (uploadField) {
              container.appendChild(uploadField);
            }
          }
        },
      },
      gallery: {
        label: "Galerie",
        defaults: () => ({ heading: "", images: [] }),
        build: (container, block, sync, context) => {
          container.appendChild(
            createLabeledInput("Überschrift", block.heading, (value) => {
              block.heading = value;
              sync();
            })
          );

          const galleryWrapper = document.createElement("div");
          galleryWrapper.className = "content-builder__gallery";

          const list = document.createElement("div");
          list.className = "content-builder__gallery-list";

          const renderImages = () => {
            list.innerHTML = "";
            if (!Array.isArray(block.images)) {
              block.images = [];
            }
            block.images.forEach((media, index) => {
              const row = document.createElement("div");
              row.className = "content-builder__gallery-row";
              row.appendChild(
                createLabeledInput("Bild (Pfad oder URL)", media.image || "", (value) => {
                  block.images[index].image = value;
                  sync();
                })
              );
              row.appendChild(
                createLabeledInput("Alt-Text", media.alt || "", (value) => {
                  block.images[index].alt = value;
                  sync();
                })
              );
              row.appendChild(
                createLabeledInput("Beschreibung", media.caption || "", (value) => {
                  block.images[index].caption = value;
                  sync();
                })
              );
              if (context && typeof context.createUploadField === "function") {
                const uploadField = context.createUploadField("Bild hochladen", [
                  "gallery",
                  index,
                  "upload",
                ]);
                if (uploadField) {
                  row.appendChild(uploadField);
                }
              }
              const remove = document.createElement("button");
              remove.type = "button";
              remove.className = "content-builder__remove";
              remove.setAttribute("aria-label", "Bild entfernen");
              remove.textContent = "×";
              remove.addEventListener("click", () => {
                block.images.splice(index, 1);
                renderImages();
                sync();
              });
              row.appendChild(remove);
              list.appendChild(row);
            });

            if (!block.images.length) {
              const empty = document.createElement("p");
              empty.className = "content-builder__gallery-empty";
              empty.textContent = "Noch keine Bilder in der Galerie.";
              list.appendChild(empty);
            }
          };

          const addButton = document.createElement("button");
          addButton.type = "button";
          addButton.className = "button button-secondary";
          addButton.textContent = "➕ Bild hinzufügen";
          addButton.addEventListener("click", () => {
            if (!Array.isArray(block.images)) {
              block.images = [];
            }
            block.images.push({ image: "", alt: "", caption: "" });
            renderImages();
            sync();
          });

          galleryWrapper.append(list, addButton);
          container.appendChild(galleryWrapper);
          renderImages();
        },
      },
      split: {
        label: "Split",
        defaults: () => ({
          heading: "",
          layout: "text-left",
          text_heading: "",
          text_body: "",
          image_heading: "",
          image: "",
          image_alt: "",
          image_caption: "",
        }),
        build: (container, block, sync, context) => {
          container.appendChild(
            createLabeledInput("Abschnittstitel", block.heading, (value) => {
              block.heading = value;
              sync();
            })
          );
          container.appendChild(
            createLabeledSelect(
              "Layout",
              sanitizeSplitLayout(block.layout),
              [
                { value: "text-left", label: "Text links / Bild rechts" },
                { value: "text-right", label: "Bild links / Text rechts" },
                { value: "image-top", label: "Bild oben / Text unten" },
                { value: "text-top", label: "Text oben / Bild unten" },
              ],
              (value) => {
                block.layout = sanitizeSplitLayout(value);
                sync();
              }
            )
          );
          container.appendChild(
            createLabeledInput("Textspalte — Überschrift", block.text_heading, (value) => {
              block.text_heading = value;
              sync();
            })
          );
          container.appendChild(
            createLabeledTextarea("Textspalte — Inhalt", block.text_body, (value) => {
              block.text_body = value;
              sync();
            }, { rows: 5 })
          );
          container.appendChild(
            createLabeledInput("Bildspalte — Überschrift", block.image_heading, (value) => {
              block.image_heading = value;
              sync();
            })
          );
          container.appendChild(
            createLabeledInput("Bild (Pfad oder URL)", block.image, (value) => {
              block.image = value;
              sync();
            })
          );
          if (context && typeof context.createUploadField === "function") {
            const uploadField = context.createUploadField("Bild hochladen", [
              "split",
              "image",
              "upload",
            ]);
            if (uploadField) {
              container.appendChild(uploadField);
            }
          }
          container.appendChild(
            createLabeledInput("Bild Alt-Text", block.image_alt, (value) => {
              block.image_alt = value;
              sync();
            })
          );
          container.appendChild(
            createLabeledTextarea("Bildbeschreibung", block.image_caption, (value) => {
              block.image_caption = value;
              sync();
            }, { rows: 2 })
          );
        },
      },
    };

    const createLabeledInput = (labelText, value, onInput, options = {}) => {
      const wrapper = document.createElement("label");
      wrapper.className = "content-builder__field";
      const span = document.createElement("span");
      span.textContent = labelText;
      const input = document.createElement("input");
      input.type = options.type || "text";
      if (options.placeholder) {
        input.placeholder = options.placeholder;
      }
      input.value = value || "";
      input.addEventListener("input", (event) => {
        onInput(event.target.value);
      });
      wrapper.append(span, input);
      return wrapper;
    };

    const createLabeledFileInput = (labelText, name, options = {}) => {
      if (!name) {
        return null;
      }
      const wrapper = document.createElement("label");
      wrapper.className = "content-builder__field";
      const span = document.createElement("span");
      span.textContent = labelText;
      const input = document.createElement("input");
      input.type = "file";
      input.name = name;
      input.accept = options.accept || "";
      wrapper.append(span, input);
      return wrapper;
    };

    const createLabeledTextarea = (labelText, value, onInput, options = {}) => {
      const wrapper = document.createElement("label");
      wrapper.className = "content-builder__field";
      const span = document.createElement("span");
      span.textContent = labelText;
      const textarea = document.createElement("textarea");
      textarea.rows = options.rows || 3;
      textarea.value = value || "";
      textarea.addEventListener("input", (event) => {
        onInput(event.target.value);
      });
      wrapper.append(span, textarea);
      return wrapper;
    };

    const createLabeledSelect = (labelText, value, options, onChange) => {
      const wrapper = document.createElement("label");
      wrapper.className = "content-builder__field";
      const span = document.createElement("span");
      span.textContent = labelText;
      const select = document.createElement("select");
      options.forEach((option) => {
        const opt = document.createElement("option");
        opt.value = option.value;
        opt.textContent = option.label;
        if (option.value === value) {
          opt.selected = true;
        }
        select.appendChild(opt);
      });
      select.addEventListener("change", (event) => {
        onChange(event.target.value);
      });
      wrapper.append(span, select);
      return wrapper;
    };

    const createDefaultBlock = (type) => {
      const config = BLOCK_CONFIG[type] || BLOCK_CONFIG.text;
      return { type, ...config.defaults() };
    };

    const normalizeBlock = (block) => {
      if (!block || typeof block !== "object") {
        return null;
      }
      const type = BLOCK_CONFIG[block.type] ? block.type : "text";
      const base = createDefaultBlock(type);
      Object.assign(base, block, { type });
      if (type === "gallery") {
        base.images = Array.isArray(base.images)
          ? base.images.map((media) => ({
              image: typeof media.image === "string" ? media.image : "",
              alt: typeof media.alt === "string" ? media.alt : "",
              caption: typeof media.caption === "string" ? media.caption : "",
            }))
          : [];
      }
      if (type === "split") {
        base.layout = sanitizeSplitLayout(base.layout);
      }
      return base;
    };

    document.querySelectorAll("[data-content-builder]").forEach((builder) => {
      if (builder.dataset.builderInitialized === "true") {
        return;
      }
      const hiddenInput = builder.querySelector('input[type="hidden"]');
      if (!hiddenInput) {
        return;
      }

      builder.dataset.builderInitialized = "true";

      const itemsContainer = builder.querySelector("[data-builder-items]");
      const emptyMessage = builder.querySelector("[data-builder-empty]");
      const addButtons = builder.querySelectorAll("[data-builder-add]");

      const parseSource = () => {
        const directValue = (hiddenInput.value || "").trim();
        if (directValue) {
          return directValue;
        }
        const dataInitial = builder.getAttribute("data-initial");
        if (dataInitial) {
          return dataInitial;
        }
        return "[]";
      };

      const parseItems = () => {
        try {
          const parsed = JSON.parse(parseSource());
          if (Array.isArray(parsed)) {
            return parsed
              .map((block) => normalizeBlock(block))
              .filter((block) => block !== null);
          }
        } catch (error) {
          // eslint-disable-next-line no-console
          console.warn("Konnte Content-Builder-Daten nicht lesen:", error);
        }
        return [];
      };

      let items = parseItems();

      const sync = () => {
        try {
          hiddenInput.value = JSON.stringify(items);
        } catch (error) {
          hiddenInput.value = "[]";
        }
        if (emptyMessage) {
          emptyMessage.hidden = items.length > 0;
        }
      };

      const render = () => {
        if (itemsContainer) {
          itemsContainer.innerHTML = "";
          items.forEach((block, index) => {
            itemsContainer.appendChild(renderBlock(block, index));
          });
        }
        sync();
      };

      const renderBlock = (block, index) => {
        const config = BLOCK_CONFIG[block.type] || BLOCK_CONFIG.text;
        const section = document.createElement("section");
        section.className = "content-builder__item";

        const header = document.createElement("header");
        header.className = "content-builder__item-header";

        const label = document.createElement("span");
        label.className = "content-builder__item-label";
        label.textContent = `${config.label} ${index + 1}`;

        const controls = document.createElement("div");
        controls.className = "content-builder__item-actions";

        const typeSelect = document.createElement("select");
        Object.entries(BLOCK_CONFIG).forEach(([key, option]) => {
          const opt = document.createElement("option");
          opt.value = key;
          opt.textContent = option.label;
          if (key === block.type) {
            opt.selected = true;
          }
          typeSelect.appendChild(opt);
        });
        typeSelect.addEventListener("change", (event) => {
          const nextType = event.target.value;
          items[index] = createDefaultBlock(nextType);
          render();
        });

        const removeButton = document.createElement("button");
        removeButton.type = "button";
        removeButton.className = "content-builder__remove";
        removeButton.setAttribute("aria-label", "Block entfernen");
        removeButton.textContent = "×";
        removeButton.addEventListener("click", () => {
          items.splice(index, 1);
          render();
        });

        controls.append(typeSelect, removeButton);
        header.append(label, controls);

        const fields = document.createElement("div");
        fields.className = "content-builder__fields";
        const uploadPrefix = builder.getAttribute("data-upload-prefix") || "";
        const context = {
          blockIndex: index,
          uploadPrefix,
          createUploadField: (labelText, parts = []) => {
            if (!uploadPrefix) {
              return null;
            }
            const nameParts = [uploadPrefix, index];
            parts.forEach((part) => {
              if (part !== undefined && part !== null) {
                nameParts.push(String(part));
              }
            });
            const name = nameParts.join("-");
            return createLabeledFileInput(labelText, name, { accept: "image/*" });
          },
        };
        (BLOCK_CONFIG[block.type] || BLOCK_CONFIG.text).build(fields, block, sync, context);

        section.append(header, fields);
        return section;
      };

      addButtons.forEach((button) => {
        button.addEventListener("click", (event) => {
          event.preventDefault();
          const type = button.getAttribute("data-builder-add") || "text";
          items.push(createDefaultBlock(type));
          render();
        });
      });

      render();
    });
  };

  initContentBuilders();

  document.querySelectorAll("[data-option-list]").forEach((list) => {
    const baseName = list.getAttribute("data-name");
    if (!baseName) {
      return;
    }
    const block = list.closest(".shop-option-block");
    const addButton = block ? block.querySelector(`[data-option-add="${baseName}"]`) : null;
    const labelPlaceholder = list.dataset.labelPlaceholder || "Bezeichnung";
    const pricePlaceholder = list.dataset.pricePlaceholder || "Preis";
    const notePlaceholder = list.dataset.notePlaceholder || "Zusatzinfo";

    const ensureRowPresence = () => {
      if (!list.querySelector("[data-option-row]")) {
        list.appendChild(createRow());
      }
    };

    const createRow = (values = {}) => {
      const row = document.createElement("div");
      row.className = "shop-option-row";
      row.dataset.optionRow = "";

      const labelInput = document.createElement("input");
      labelInput.type = "text";
      labelInput.name = `${baseName}-label`;
      labelInput.placeholder = labelPlaceholder;
      labelInput.value = values.label || "";

      const priceInput = document.createElement("input");
      priceInput.type = "number";
      priceInput.step = "0.01";
      priceInput.min = "0";
      priceInput.name = `${baseName}-price`;
      priceInput.placeholder = pricePlaceholder;
      if (values.price !== undefined && values.price !== null && values.price !== "") {
        priceInput.value = values.price;
      }

      const noteInput = document.createElement("input");
      noteInput.type = "text";
      noteInput.name = `${baseName}-note`;
      noteInput.placeholder = notePlaceholder;
      noteInput.value = values.note || "";

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "shop-option-remove";
      remove.setAttribute("aria-label", "Option entfernen");
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        row.remove();
        ensureRowPresence();
      });

      row.append(labelInput, priceInput, noteInput, remove);
      return row;
    };

    list.querySelectorAll(".shop-option-row").forEach((existingRow) => {
      existingRow.dataset.optionRow = "";
      const inputs = existingRow.querySelectorAll("input");
      const [labelInput, priceInput, noteInput] = inputs;

      if (!inputs.length) {
        const replacement = createRow();
        existingRow.replaceWith(replacement);
        return;
      }

      if (labelInput) {
        labelInput.name = `${baseName}-label`;
        if (!labelInput.placeholder) {
          labelInput.placeholder = labelPlaceholder;
        }
      }

      if (priceInput) {
        priceInput.name = `${baseName}-price`;
        priceInput.type = "number";
        priceInput.step = "0.01";
        priceInput.min = "0";
        if (!priceInput.placeholder) {
          priceInput.placeholder = pricePlaceholder;
        }
      }

      if (noteInput) {
        noteInput.name = `${baseName}-note`;
        if (!noteInput.placeholder) {
          noteInput.placeholder = notePlaceholder;
        }
      } else if (inputs.length === 2) {
        // Legacy rows without note input
        const insertedNote = document.createElement("input");
        insertedNote.type = "text";
        insertedNote.name = `${baseName}-note`;
        insertedNote.placeholder = notePlaceholder;
        existingRow.insertBefore(insertedNote, existingRow.querySelector(".shop-option-remove"));
      }

      const removeButton = existingRow.querySelector(".shop-option-remove");
      if (removeButton) {
        removeButton.addEventListener("click", () => {
          existingRow.remove();
          ensureRowPresence();
        });
      }
    });

    if (addButton) {
      addButton.addEventListener("click", () => {
        const row = createRow();
        list.appendChild(row);
        const firstInput = row.querySelector("input");
        if (firstInput) {
          firstInput.focus();
        }
      });
    }

    ensureRowPresence();
  });

  document.querySelectorAll("[data-purchase-widget]").forEach((widget) => {
    const currencySymbol = widget.dataset.currencySymbol || "€";
    const basePrice = parseFloat(widget.dataset.basePrice || "0") || 0;
    const quantityInput = widget.querySelector("[data-quantity]");
    const unitOutput = widget.querySelector("[data-unit-price]");
    const totalOutput = widget.querySelector("[data-total-price]");
    const stockLimit = parseInt(widget.dataset.stock || "", 10);

    const parseOptionPrice = (input) => {
      if (!input) {
        return Number.isFinite(basePrice) ? basePrice : 0;
      }
      const raw = input.getAttribute("data-option-price");
      if (!raw) {
        return Number.isFinite(basePrice) ? basePrice : 0;
      }
      const parsed = parseFloat(raw);
      return Number.isFinite(parsed) ? parsed : basePrice;
    };

    const formatPrice = (value) => {
      if (!Number.isFinite(value)) {
        value = 0;
      }
      const fixed = Math.round(value * 100) / 100;
      const euros = Math.trunc(fixed);
      const cents = Math.round((fixed - euros) * 100);
      const thousands = euros.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
      const centsText = cents.toString().padStart(2, "0");
      return `${currencySymbol}${thousands},${centsText}`;
    };

    const optionInputs = Array.from(
      widget.querySelectorAll('input[name="shop-option-choice"]')
    );

    const getSelectedOption = () =>
      optionInputs.find((input) => input.checked) || optionInputs[0] || null;

    const getQuantityLimits = () => {
      const explicitMax = parseInt(quantityInput?.dataset.max || "", 10);
      const limits = [];
      if (Number.isInteger(explicitMax) && explicitMax > 0) {
        limits.push(explicitMax);
      }
      if (Number.isInteger(stockLimit) && stockLimit > 0) {
        limits.push(stockLimit);
      }
      if (!limits.length) {
        return null;
      }
      return Math.min(...limits);
    };

    const clampQuantity = () => {
      if (!quantityInput) {
        return 1;
      }
      const max = getQuantityLimits();
      let value = parseInt(quantityInput.value || "1", 10);
      if (!Number.isInteger(value) || value <= 0) {
        value = 1;
      }
      if (max && value > max) {
        value = max;
      }
      quantityInput.value = String(value);
      return value;
    };

    const updatePrices = () => {
      const selected = getSelectedOption();
      const optionPrice = parseOptionPrice(selected);
      const quantity = clampQuantity();
      if (unitOutput) {
        unitOutput.textContent = formatPrice(optionPrice);
      }
      if (totalOutput) {
        totalOutput.textContent = formatPrice(optionPrice * quantity);
      }
    };

    optionInputs.forEach((input) => {
      input.addEventListener("change", updatePrices);
    });

    if (quantityInput) {
      quantityInput.addEventListener("input", updatePrices);
      quantityInput.addEventListener("change", updatePrices);
    }

    updatePrices();
  });

  const heroVisual = document.querySelector(".about-hero-visual");
  if (heroVisual) {
    heroVisual.addEventListener("click", () => {
      heroVisual.classList.remove("is-pulsing");
      void heroVisual.offsetWidth;
      heroVisual.classList.add("is-pulsing");
    });
  }

  const heroImage = document.querySelector(".about-hero-visual img");
  if (heroImage) {
    const offsetRange = 12;
    const scaleRange = 0.018;
    let current = { x: 0, y: 0, scale: 1 };
    let target = { x: 0, y: 0, scale: 1 };

    const pickTarget = () => {
      target = {
        x: (Math.random() - 0.5) * offsetRange,
        y: (Math.random() - 0.5) * offsetRange,
        scale: 1 + (Math.random() - 0.5) * scaleRange
      };
    };

    let heroLast = performance.now();
    const animateHero = (timestamp) => {
      const delta = Math.min(80, timestamp - heroLast);
      heroLast = timestamp;
      const ease = Math.min(0.06, 0.012 + (delta / 1000) * 0.25);

      current.x += (target.x - current.x) * ease;
      current.y += (target.y - current.y) * ease;
      current.scale += (target.scale - current.scale) * ease;

      heroImage.style.transform = `translate3d(${current.x}px, ${current.y}px, 0) scale(${current.scale})`;

      if (
        Math.abs(current.x - target.x) < 0.25 &&
        Math.abs(current.y - target.y) < 0.25 &&
        Math.abs(current.scale - target.scale) < 0.002
      ) {
        pickTarget();
      }

      requestAnimationFrame(animateHero);
    };

    pickTarget();
    requestAnimationFrame((time) => {
      heroLast = time;
      animateHero(time);
    });
  }

  const canvas = document.getElementById("starfield") || document.getElementById("starfield-canvas");
  if (!canvas) {
    return;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return;
  }

  const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let width = 0;
  let height = 0;
  let animationFrame = null;
  let shootingTimer = null;

  const randomColor = () => {
    const colors = ["#ffffff", "#e8d5ff", "#00f5ff", "#ff69d4", "#ffd700"];
    return colors[Math.floor(Math.random() * colors.length)];
  };

  const stars = [];
  const shootingStars = [];

  const resize = () => {
    width = window.innerWidth;
    height = window.innerHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);

    const targetCount = Math.max(140, Math.floor((width * height) / 9000));
    stars.length = 0;
    for (let i = 0; i < targetCount; i += 1) {
      stars.push({
        x: Math.random() * width,
        y: Math.random() * height,
        r: Math.random() * 1.8 + 0.2,
        opacity: Math.random(),
        speed: Math.random() * 0.4 + 0.05,
        twinkleSpeed: Math.random() * 0.02 + 0.005,
        twinkleDir: Math.random() > 0.5 ? 1 : -1,
        color: randomColor()
      });
    }
  };

  const spawnShootingStar = () => {
    if (prefersReducedMotion || document.hidden) {
      return;
    }
    shootingStars.push({
      x: Math.random() * width * 0.7,
      y: Math.random() * height * 0.4,
      len: Math.random() * 120 + 60,
      speed: Math.random() * 8 + 6,
      opacity: 1,
      angle: Math.PI / 4 + (Math.random() - 0.5) * 0.3
    });
  };

  const drawNebula = () => {
    [
      [width * 0.2, height * 0.3, 300, [107, 33, 200, 0.04]],
      [width * 0.75, height * 0.6, 250, [255, 105, 212, 0.03]],
      [width * 0.5, height * 0.8, 200, [0, 245, 255, 0.025]]
    ].forEach(([nx, ny, nr, [r, g, b, a]]) => {
      const grad = ctx.createRadialGradient(nx, ny, 0, nx, ny, nr);
      grad.addColorStop(0, `rgba(${r},${g},${b},${a})`);
      grad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(nx, ny, nr, 0, Math.PI * 2);
      ctx.fill();
    });
  };

  const animate = () => {
    ctx.clearRect(0, 0, width, height);
    drawNebula();

    for (const s of stars) {
      s.opacity += s.twinkleSpeed * s.twinkleDir;
      if (s.opacity >= 1 || s.opacity <= 0.1) {
        s.twinkleDir *= -1;
      }
      ctx.globalAlpha = s.opacity;
      ctx.fillStyle = s.color;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();
      s.y += s.speed * 0.03;
      if (s.y > height) {
        s.y = 0;
        s.x = Math.random() * width;
      }
    }

    for (let i = shootingStars.length - 1; i >= 0; i -= 1) {
      const ss = shootingStars[i];
      ctx.globalAlpha = ss.opacity;
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(ss.x, ss.y);
      ctx.lineTo(ss.x - Math.cos(ss.angle) * ss.len, ss.y - Math.sin(ss.angle) * ss.len);
      ctx.stroke();
      ss.x += Math.cos(ss.angle) * ss.speed;
      ss.y += Math.sin(ss.angle) * ss.speed;
      ss.opacity -= 0.025;
      if (ss.opacity <= 0) {
        shootingStars.splice(i, 1);
      }
    }

    ctx.globalAlpha = 1;
    animationFrame = requestAnimationFrame(animate);
  };

  const scheduleShootingStar = () => {
    if (shootingTimer) {
      window.clearTimeout(shootingTimer);
    }
    shootingTimer = window.setTimeout(() => {
      spawnShootingStar();
      scheduleShootingStar();
    }, 2800 + Math.random() * 1200);
  };

  resize();
  window.addEventListener("resize", resize, { passive: true });
  scheduleShootingStar();
  animate();

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (animationFrame) {
        cancelAnimationFrame(animationFrame);
        animationFrame = null;
      }
    } else if (!animationFrame) {
      animate();
    }
  });
});
