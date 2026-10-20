(function () {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  document.querySelectorAll(".js-audio-dictation-button").forEach(function (button) {
    const input = document.getElementById(button.dataset.inputId);
    const status = document.getElementById(button.dataset.statusId);
    if (!input || !status) return;
    if (!SpeechRecognition) {
      button.disabled = true;
      button.title = "El reconocimiento de voz no está disponible en este navegador";
      status.textContent = "El dictado no está disponible en este navegador.";
      return;
    }

  const recognition = new SpeechRecognition();
  recognition.lang = "es-ES";
  recognition.continuous = true;
  recognition.interimResults = true;
  let listening = false;
  let baseText = "";
  let finalText = "";
  let stopAfterResult = false;
  let noResultTimer = null;

  function spokenToQuick(text) {
    let value = text.replace(/\bfin\b[.!?]*\s*$/i, "").trim();
    const boundaries = "(?:etiqueta|etiquetas|tag|proyecto|carpeta|prioridad|hoy|mañana|\\+\\s*\\d+|\\d{1,2}[-/]\\d{1,2}(?:[-/]\\d{4})?|a las|\\bh:?\\s*\\d{1,2}\\s*(?::|y)\\s*\\d{2})";
    function replaceNamed(kind, token) {
      const pattern = new RegExp("\\b(?:" + kind + ")(?:\\s+)(.+?)(?=\\s+" + boundaries + "\\b|$)", "ig");
      value = value.replace(pattern, function (_, name) {
        name = name.trim().replace(/[.,;:!?]+$/, "");
        if (!name) return _;
        const clean = name.replace(/^(?:arroba|almohadilla|gato|ef[eé] dos puntos)\s+/i, "").trim();
        if (!clean) return _;
        const escaped = clean.replace(/\s+/g, " ");
        return token === "@" ? "@" + escaped.replace(/\s+/g, "") : token + '"' + escaped.replace(/"/g, "") + '"';
      });
    }
    replaceNamed("etiquetas?", "@");
    replaceNamed("tags?", "@");
    replaceNamed("proyecto", "#");
    replaceNamed("carpeta", "f:");

    value = value.replace(/\bsiguiente\b/gi, "@NextAction");
    value = value.replace(/\b(hoy|mañana)\b/gi, "*$1");

    value = value.replace(/\bhora\s+(?:h\s*)?(\d{1,2})\s*(?::|y)\s*(\d{2})\b/gi, function (_, h, m) {
      return "h:" + String(h).padStart(2, "0") + ":" + m;
    });
    const spokenHours = { una: 1, uno: 1, dos: 2, tres: 3, cuatro: 4, cinco: 5, seis: 6, siete: 7, ocho: 8, nueve: 9, diez: 10, once: 11, doce: 12 };
    value = value.replace(/\ba las (\d{1,2}|una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)(?:\s+y\s+(media|cuarto))?\b/gi, function (_, spokenHour, fraction) {
      let hour = Number(spokenHour) || spokenHours[spokenHour.toLowerCase()];
      let minute = fraction === "media" ? "30" : fraction === "cuarto" ? "15" : "00";
      if (hour >= 1 && hour <= 12 && /tarde|noche/i.test(value) && hour < 12) hour += 12;
      return "h:" + String(hour).padStart(2, "0") + ":" + minute;
    });
    value = value.replace(/\bprioridad\s+(alta|media|baja)\b/gi, "^$1");
    value = value.replace(/\b(arroba|at)\s+([A-Za-z0-9_áéíóúÁÉÍÓÚñÑ-]+)/gi, "@$2");
    value = value.replace(/\b(?:almohadilla|gato)\s+([A-Za-z0-9_áéíóúÁÉÍÓÚñÑ-]+)/gi, "#$1");
    return value.replace(/\s+/g, " ").trim();
  }

  function updateInput(interimText) {
    const dictated = [finalText, interimText].filter(Boolean).join(" ");
    const normalized = spokenToQuick(dictated);
    input.value = [baseText, normalized].filter(Boolean).join(" ");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  button.addEventListener("click", function () {
    if (listening) {
      recognition.stop();
      return;
    }
    if (button.dataset.resetForm === "true" && input.form) input.form.reset();
    baseText = input.value.trim();
    finalText = "";
    stopAfterResult = false;
    status.textContent = "Escuchando… habla ahora.";
    try { recognition.start(); }
    catch (error) { status.textContent = "No se pudo iniciar el dictado: " + error.message; }
  });

  recognition.onstart = function () {
    listening = true;
    button.textContent = "⏹ Detener";
    button.setAttribute("aria-label", "Detener dictado");
    clearTimeout(noResultTimer);
    noResultTimer = setTimeout(function () {
      status.textContent = "No llegaron resultados del reconocimiento de voz. Comprueba la conexión y los permisos del micrófono e inténtalo de nuevo.";
      recognition.stop();
    }, 20000);
  };
  recognition.onresult = function (event) {
    clearTimeout(noResultTimer);
    let interimText = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript.trim();
      if (event.results[i].isFinal) {
        if (/\bfin\b[.!?]*\s*$/i.test(transcript)) {
          const withoutFin = transcript.replace(/\bfin\b[.!?]*\s*$/i, "").trim();
          finalText += (finalText && withoutFin ? " " : "") + withoutFin;
          stopAfterResult = true;
        } else {
          finalText += (finalText ? " " : "") + transcript;
        }
      }
      else interimText += (interimText ? " " : "") + transcript;
    }
    try {
      updateInput(interimText);
    } catch (error) {
      status.textContent = "No se pudo procesar la transcripción: " + error.message;
      recognition.stop();
      return;
    }
    if (stopAfterResult) {
      if (!/(?:^|\s)@inbox\.audio(?:\s|$)/i.test(input.value)) {
        input.value = (input.value.trim() + " @inbox.audio").trim();
      }
      status.textContent = "Se detectó «FIN»; dictado detenido y se añadió @inbox.audio. Revisa el texto antes de añadirlo.";
      recognition.stop();
    }
  };
  recognition.onerror = function (event) {
    clearTimeout(noResultTimer);
    const messages = {
      "not-allowed": "No hay permiso para usar el micrófono.",
      "no-speech": "No se detectó voz. Puedes volver a intentarlo.",
      "audio-capture": "No se encontró un micrófono disponible.",
      "network": "No está disponible el servicio de reconocimiento de voz del navegador. Comprueba la conexión e inténtalo de nuevo.",
      "service-not-allowed": "No se permite usar el servicio de reconocimiento de voz del navegador."
    };
    status.textContent = messages[event.error] || "Hubo un problema con el reconocimiento de voz.";
  };
  recognition.onend = function () {
    clearTimeout(noResultTimer);
    listening = false;
    button.textContent = "🎙️ Dictar";
    button.setAttribute("aria-label", "Dictar tarea");
    if (!status.textContent.startsWith("No ") && !status.textContent.startsWith("Hubo ") && !status.textContent.startsWith("Se detectó «FIN»")) {
      status.textContent = input.value.trim() ? "Dictado listo; revisa el texto y pulsa «Añadir a inbox»." : "Dictado detenido.";
    }
  };
  });
})();
