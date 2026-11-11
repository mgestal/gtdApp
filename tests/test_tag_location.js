const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const elements = {};
for (const id of ["location-enabled", "location-latitude", "location-longitude",
  "location-radius", "tag-location-status", "location-use-current"]) {
  elements[id] = {checked: false, value: "", disabled: false, required: false, textContent: "",
    addEventListener(name, callback) { this[name] = callback; }};
}
let fail = false;
vm.runInNewContext(fs.readFileSync("static/tag_location.js", "utf8"), {
  document: {getElementById: id => elements[id]},
  window: {isSecureContext: true},
  navigator: {geolocation: {getCurrentPosition(success, failure) {
    if (fail) failure({message: "Permiso denegado"});
    else success({coords: {latitude: 42.12345678, longitude: -8.12345678, accuracy: 25}});
  }}}
});
assert.equal(elements["location-latitude"].disabled, true);
elements["location-use-current"].click();
assert.equal(elements["location-enabled"].checked, true);
assert.equal(elements["location-latitude"].value, "42.1234568");
assert.equal(elements["location-longitude"].value, "-8.1234568");
assert.equal(elements["location-radius"].required, true);
assert.match(elements["tag-location-status"].textContent, /25 m/);
elements["location-enabled"].checked = false;
elements["location-enabled"].change();
assert.equal(elements["location-latitude"].disabled, true);
fail = true;
elements["location-use-current"].click();
assert.match(elements["tag-location-status"].textContent, /Permiso denegado/);
assert.equal(elements["location-use-current"].disabled, false);
console.log("Configuración de ubicación de etiquetas OK");
