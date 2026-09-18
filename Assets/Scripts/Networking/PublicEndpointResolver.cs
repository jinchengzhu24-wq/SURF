using System;
using UnityEngine;

public static class PublicEndpointResolver
{
    public const string ProductionOrigin = "https://sokobanaidemo.top";
    private const string ProductionHost = "sokobanaidemo.top";
    private const string LegacyPublicHost = "111.231.136.4";

    public static string ResolveBackendBaseUrl(string configuredFallback = null)
    {
        return ResolveServiceBaseUrl(configuredFallback, "");
    }

    public static string ResolveCoCreationBaseUrl(string configuredFallback = null)
    {
        return ResolveServiceBaseUrl(configuredFallback, "/cocreation");
    }

    public static string ResolvePublicUrl(string configuredFallback, string canonicalPath)
    {
#if UNITY_WEBGL && !UNITY_EDITOR
        if (TryGetHttpOrigin(Application.absoluteURL, out string browserOrigin))
        {
            return JoinOriginAndPath(browserOrigin, canonicalPath);
        }
#endif
        return TryGetHttpUrl(configuredFallback, out string configuredUrl)
            ? configuredUrl
            : JoinOriginAndPath(ProductionOrigin, canonicalPath);
    }

    public static string ResolveEndpoint(string configuredFallback, string canonicalPath)
    {
        return ResolvePublicUrl(configuredFallback, canonicalPath);
    }

    public static string ResolveOriginForPageUrl(
        string pageUrl,
        string configuredFallback = null)
    {
        if (TryGetHttpOrigin(pageUrl, out string pageOrigin))
        {
            return pageOrigin;
        }

        if (TryGetHttpUrl(configuredFallback, out string configuredUrl)
            && TryGetHttpOrigin(configuredUrl, out string configuredOrigin))
        {
            return configuredOrigin;
        }

        return ProductionOrigin;
    }

    public static bool TryGetHttpOrigin(string value, out string origin)
    {
        origin = "";
        if (!Uri.TryCreate(value, UriKind.Absolute, out Uri uri)
            || (uri.Scheme != Uri.UriSchemeHttp
                && uri.Scheme != Uri.UriSchemeHttps)
            || string.IsNullOrWhiteSpace(uri.Host))
        {
            return false;
        }

        origin = NormalizeProductionScheme(uri)
            .GetLeftPart(UriPartial.Authority)
            .TrimEnd('/');
        return true;
    }

    private static string ResolveServiceBaseUrl(
        string configuredFallback,
        string servicePath)
    {
#if UNITY_WEBGL && !UNITY_EDITOR
        if (TryGetHttpOrigin(Application.absoluteURL, out string browserOrigin))
        {
            return JoinOriginAndPath(browserOrigin, servicePath);
        }
#endif
        return TryGetHttpUrl(configuredFallback, out string configuredUrl)
            ? configuredUrl.TrimEnd('/')
            : JoinOriginAndPath(ProductionOrigin, servicePath);
    }

    private static bool TryGetHttpUrl(string value, out string resolvedUrl)
    {
        resolvedUrl = "";
        if (!Uri.TryCreate(value, UriKind.Absolute, out Uri uri)
            || (uri.Scheme != Uri.UriSchemeHttp
                && uri.Scheme != Uri.UriSchemeHttps)
            || string.IsNullOrWhiteSpace(uri.Host)
            || string.Equals(
                uri.Host,
                LegacyPublicHost,
                StringComparison.OrdinalIgnoreCase
            ))
        {
            return false;
        }

        resolvedUrl = NormalizeProductionScheme(uri).AbsoluteUri.TrimEnd('/');
        return true;
    }

    private static Uri NormalizeProductionScheme(Uri uri)
    {
        if (uri.Scheme == Uri.UriSchemeHttp
            && string.Equals(
                uri.Host,
                ProductionHost,
                StringComparison.OrdinalIgnoreCase
            ))
        {
            return new UriBuilder(uri)
            {
                Scheme = Uri.UriSchemeHttps,
                Port = -1,
            }.Uri;
        }

        return uri;
    }

    private static string JoinOriginAndPath(string origin, string path)
    {
        string normalizedOrigin = origin.TrimEnd('/');
        string normalizedPath = string.IsNullOrWhiteSpace(path)
            ? ""
            : "/" + path.Trim('/');
        return normalizedOrigin + normalizedPath;
    }
}
