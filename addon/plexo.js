// Shared by the parser: running the bridge and turning its answer into what FDM expects.
//
// FDM hands a link to this add-on, which asks the bridge (bridge/plexo_fdm.py, run with the Python
// FDM installs for add-ons) to take it. The bridge answers with a link on this computer
// (http://127.0.0.1:47913/d/...) that FDM then downloads as usual, while the bridge fetches the
// file over every network at once. When splitting wouldn't help (a small file, one network, a
// server that can't send parts of a file) the add-on declines and FDM downloads the link itself.

var PLEXO_BRIDGE_SCRIPT = "bridge/plexo_fdm.py";
var PLEXO_BRIDGE_PORT = 47913;
var PLEXO_MARKER = /#plexo$/i;
// The bridge has the last word (its "Smallest file" setting); this only spares it obvious cases.
var PLEXO_MIN_HINT_BYTES = 8 * 1024 * 1024;

function plexoIsBridgeUrl(url)
{
    var m = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(?::(\d+))?\//i.exec(String(url));
    return !!m && Number(m[2] || 80) === PLEXO_BRIDGE_PORT;
}

function plexoLog(text)
{
    try { console.log("Plexo: " + text); } catch (e) {}
}

// FDM's cookies, as JSON, in a temporary file the bridge deletes once read: on the command line
// other programs on the computer could see them.
function plexoCookiesFile(obj)
{
    if (!obj.cookies || !obj.cookies.length)
        return null;
    var cookies = [];
    for (var i = 0; i < obj.cookies.length; ++i)
    {
        var c = obj.cookies[i];
        var expires = 0;
        try { expires = c.expirationDate && c.expirationDate.getTime() ? c.expirationDate.getTime() / 1000 : 0; }
        catch (e) {}
        cookies.push({
            domain: String(c.domain || ""),
            path: String(c.path || "/"),
            name: String(c.name),
            value: String(c.value),
            isSecure: !!c.isSecure,
            expires: expires
        });
    }
    try
    {
        var file = qtJsTools.createTmpFile("plexo_" + obj.requestId + "_cookies");
        if (file && file.writeText(JSON.stringify(cookies)))
            return file.path;
    }
    catch (e)
    {
        plexoLog("couldn't pass cookies: " + e);
    }
    return null;
}

function plexoArgs(obj)
{
    var args = ["register", "--url", String(obj.url)];
    if (typeof obj.resourceSize === "number" && obj.resourceSize >= 0)
        args.push("--size", String(obj.resourceSize));
    var userAgent = obj.userAgent;
    if (!userAgent)
    {
        try { userAgent = qtJsSystem.defaultUserAgent; } catch (e) {}
    }
    if (userAgent)
        args.push("--user-agent", String(userAgent));
    var referer = obj.referer || obj.referrer;
    if (referer)
        args.push("--referer", String(referer));
    var cookies = plexoCookiesFile(obj);
    if (cookies)
        args.push("--cookies-file", cookies);
    return args;
}

// The last line of the script's output that is a JSON object: the bridge's answer.
function plexoParseOutput(result)
{
    if (result && result.errorOutput)
        plexoLog("bridge stderr: " + result.errorOutput);
    var lines = String((result && result.output) || "").trim().split(/\r?\n/);
    for (var i = lines.length - 1; i >= 0; --i)
    {
        var line = lines[i].trim();
        if (line.charAt(0) !== "{")
            continue;
        try { return JSON.parse(line); } catch (e) {}
    }
    return { ok: false, reason: "error", message: "The bridge gave no answer (exit code " + (result ? result.exitCode : "?") + ")" };
}

function plexoSplitName(filename)
{
    var name = String(filename || "download");
    var m = /^(.+)\.([A-Za-z0-9]{1,10})$/.exec(name);
    if (m)
        return { title: m[1], ext: m[2].toLowerCase() };
    return { title: name, ext: "bin" };
}

// What FDM's media parsers return: one "format", the bridge's link to the file.
function plexoMediaInfo(sourceUrl, answer)
{
    var name = plexoSplitName(answer.filename);
    var networks = (answer.networks || []).join(" + ");
    var format = {
        url: answer.url,
        protocol: "http",
        ext: name.ext,
        format_id: "plexo",
        format_note: networks ? "Plexo: " + networks : "Plexo"
    };
    if (answer.size > 0)
        format.filesize = answer.size;
    return {
        title: name.title,
        webpage_url: String(sourceUrl).replace(PLEXO_MARKER, ""),
        formats: [format]
    };
}

// The proxy FDM is set to use for `url`, if any. The bridge connects straight out through each
// network, which would go around it.
function plexoProxyFor(url)
{
    try
    {
        return String(qtJsNetworkProxyMgr.proxyForUrl(url).url() || "");
    }
    catch (e)
    {
        return "";
    }
}

function plexoRegister(obj)
{
    if (!PLEXO_MARKER.test(String(obj.url)) && plexoProxyFor(obj.url))
    {
        plexoLog("FDM uses a proxy for this link; leaving it to FDM");
        return Promise.reject({ error: "Unsupported URL", isParseError: false });
    }
    var requestId = obj && obj.requestId ? obj.requestId : 0;
    var interactive = !!(obj && obj.interactive);
    plexoLog("asking the bridge about " + obj.url);
    return launchPythonScript(requestId, interactive, PLEXO_BRIDGE_SCRIPT, plexoArgs(obj))
        .then(function (result)
        {
            var answer = plexoParseOutput(result);
            if (!answer.ok)
            {
                plexoLog("not splitting: " + (answer.message || answer.reason));
                // "Unsupported URL" without isParseError: FDM downloads the link the usual way.
                return Promise.reject({
                    error: answer.reason === "error" && PLEXO_MARKER.test(String(obj.url))
                        ? "Plexo bridge: " + answer.message
                        : "Unsupported URL",
                    isParseError: answer.reason === "error" && PLEXO_MARKER.test(String(obj.url))
                });
            }
            plexoLog("splitting over " + (answer.networks || []).join(", ") + "; status page: " + answer.ui);
            return plexoMediaInfo(obj.url, answer);
        });
}
