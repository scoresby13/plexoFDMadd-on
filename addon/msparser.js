// The media parser FDM calls for a link: decides which links to offer the bridge.

var msParser = (function()
{
    function MsParser()
    {
    }

    MsParser.prototype = {

        parse: function (obj)
        {
            return plexoRegister(obj);
        },

        // A link ending in #plexo always goes through the bridge.
        isSupportedSource: function (url)
        {
            return /^https?:\/\//i.test(url) && PLEXO_MARKER.test(url) && !plexoIsBridgeUrl(url);
        },

        // Asked first, before the other add-ons: the bridge only takes plain files, never web
        // pages, so a video site's page still reaches a video add-on.
        supportedSourceCheckPriority: function ()
        {
            return 0x7FFFFFFF - 2;
        },

        // Any other large file, once FDM knows its type and size. The bridge decides for sure.
        isPossiblySupportedSource: function (obj)
        {
            var url = String(obj.url || "");
            if (!/^https?:\/\//i.test(url) || plexoIsBridgeUrl(url))
                return false;
            if (obj.contentType && /^(text\/html|application\/xhtml\+xml)\b/i.test(obj.contentType))
                return false;
            if (typeof obj.resourceSize === "number" && obj.resourceSize !== -1 &&
                    obj.resourceSize < PLEXO_MIN_HINT_BYTES)
            {
                return false;
            }
            return true;
        },

        minIntevalBetweenQueryInfoDownloads: function ()
        {
            return 300;
        }
    };

    return new MsParser();
}());
