// Make external links open in new tab
document.addEventListener('DOMContentLoaded', function() {
    // Find all links in the document
    var links = document.querySelectorAll('a[href^="http"]');

    links.forEach(function(link) {
        // Check if the link is external (not pointing to the current domain)
        if (link.hostname !== window.location.hostname) {
            link.setAttribute('target', '_blank');
            link.setAttribute('rel', 'noopener noreferrer');
        }
    });
});
