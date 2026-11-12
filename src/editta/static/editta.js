// A request that gets no answer at all (server down, connection lost) fires
// htmx:sendError and nothing is swapped, so without this the page stays silent.
// A failed poll writes to #poll-error, which the next good poll clears
// (_job_list_response.html); anything else (an upload, sign-out) to #htmx-error.
document.addEventListener("htmx:sendError", function (event) {
  var poll = event.detail.elt.hasAttribute("hx-get");
  var box = document.getElementById(poll ? "poll-error" : "htmx-error");
  if (!box) return;
  var text = poll
    ? "Can't reach Editta right now. This list will update when the connection is back."
    : "Couldn't reach Editta. Check your connection and try again.";
  // Rewriting the same text would make a screen reader announce it on every poll.
  if (box.textContent !== text) box.textContent = text;
});
