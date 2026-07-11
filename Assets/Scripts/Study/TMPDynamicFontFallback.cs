using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.TextCore.LowLevel;

public static class TMPDynamicFontFallback
{
    private static readonly Dictionary<Font, TMP_FontAsset> FontAssets =
        new Dictionary<Font, TMP_FontAsset>();

    public static void Add(TMP_FontAsset primaryFontAsset, Font fallbackFont)
    {
        if (primaryFontAsset == null || fallbackFont == null)
        {
            return;
        }

        TMP_FontAsset fallbackFontAsset = GetOrCreate(fallbackFont);

        if (fallbackFontAsset == null)
        {
            return;
        }

        if (primaryFontAsset.fallbackFontAssetTable == null)
        {
            primaryFontAsset.fallbackFontAssetTable = new List<TMP_FontAsset>();
        }

        primaryFontAsset.fallbackFontAssetTable.Remove(fallbackFontAsset);
        primaryFontAsset.fallbackFontAssetTable.Insert(0, fallbackFontAsset);
    }

    private static TMP_FontAsset GetOrCreate(Font font)
    {
        if (FontAssets.TryGetValue(font, out TMP_FontAsset fontAsset) && fontAsset != null)
        {
            return fontAsset;
        }

        fontAsset = TMP_FontAsset.CreateFontAsset(
            font,
            96,
            8,
            GlyphRenderMode.SDFAA,
            2048,
            2048,
            AtlasPopulationMode.Dynamic,
            true
        );

        if (fontAsset == null)
        {
            Debug.LogWarning("TMPDynamicFontFallback: Failed to create a dynamic font asset for " + font.name + ".");
            return null;
        }

        fontAsset.name = font.name + " TMP Dynamic Fallback";
        fontAsset.isMultiAtlasTexturesEnabled = true;
        FontAssets[font] = fontAsset;
        return fontAsset;
    }
}
