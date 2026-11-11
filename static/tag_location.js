(function () {
  const enabled = document.getElementById("location-enabled");
  const latitude = document.getElementById("location-latitude");
  const longitude = document.getElementById("location-longitude");
  const radius = document.getElementById("location-radius");
  const status = document.getElementById("tag-location-status");
  const button = document.getElementById("location-use-current");

  function updateFields() {
    for (const field of [latitude, longitude, radius]) {
      field.disabled = !enabled.checked;
      field.required = enabled.checked;
    }
  }
  enabled.addEventListener("change", updateFields);
  updateFields();

  button.addEventListener("click", () => {
    if (!window.isSecureContext || !navigator.geolocation) {
      status.textContent = "La ubicación requiere HTTPS y un navegador compatible.";
      return;
    }
    status.textContent = "Obteniendo ubicación...";
    button.disabled = true;
    navigator.geolocation.getCurrentPosition(position => {
      latitude.value = position.coords.latitude.toFixed(7);
      longitude.value = position.coords.longitude.toFixed(7);
      enabled.checked = true;
      updateFields();
      status.textContent = `Ubicación obtenida; precisión aproximada: ${Math.round(position.coords.accuracy)} m. Revisa las coordenadas antes de guardar.`;
      button.disabled = false;
    }, error => {
      status.textContent = `No se pudo obtener la ubicación: ${error.message || "comprueba los permisos y la conexión"}.`;
      button.disabled = false;
    }, {enableHighAccuracy: true, timeout: 15000, maximumAge: 0});
  });
})();
