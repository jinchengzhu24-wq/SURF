using System;
using UnityEngine;

[Serializable]
public class PCDesignSketchData
{
    public int version = PCDesignContext.CurrentVersion;
    public int width;
    public int height;
    public string[] rows;
}

public static class PCDesignContext
{
    public const int CurrentVersion = 1;
    private const string SketchPrefsKey = "SokobanPCDesignSketch";
    private const string RestorePrefsKey = "SokobanPCDesignRestoreRequested";

    public static bool Save(string[] rows, int width, int height)
    {
        if (!AreRowsValid(rows, width, height))
        {
            return false;
        }

        PCDesignSketchData data = new PCDesignSketchData
        {
            version = CurrentVersion,
            width = width,
            height = height,
            rows = CloneRows(rows)
        };

        PlayerPrefs.SetString(SketchPrefsKey, JsonUtility.ToJson(data));
        PlayerPrefs.DeleteKey(RestorePrefsKey);
        PlayerPrefs.Save();
        return true;
    }

    public static bool TryLoad(out PCDesignSketchData data)
    {
        data = null;
        string json = PlayerPrefs.GetString(SketchPrefsKey, "");

        if (string.IsNullOrWhiteSpace(json))
        {
            return false;
        }

        try
        {
            data = JsonUtility.FromJson<PCDesignSketchData>(json);
        }
        catch (Exception)
        {
            data = null;
        }

        if (data == null
            || data.version != CurrentVersion
            || !AreRowsValid(data.rows, data.width, data.height))
        {
            data = null;
            return false;
        }

        data.rows = CloneRows(data.rows);
        return true;
    }

    public static void RequestDesignRestore()
    {
        PlayerPrefs.SetInt(RestorePrefsKey, 1);
        PlayerPrefs.Save();
    }

    public static bool ConsumeDesignRestoreRequest()
    {
        bool requested = PlayerPrefs.GetInt(RestorePrefsKey, 0) == 1;
        PlayerPrefs.DeleteKey(RestorePrefsKey);
        PlayerPrefs.Save();
        return requested;
    }

    public static void Clear()
    {
        PlayerPrefs.DeleteKey(SketchPrefsKey);
        PlayerPrefs.DeleteKey(RestorePrefsKey);
        PlayerPrefs.Save();
    }

    private static bool AreRowsValid(string[] rows, int width, int height)
    {
        if (width <= 0 || height <= 0 || rows == null || rows.Length != height)
        {
            return false;
        }

        for (int row = 0; row < rows.Length; row++)
        {
            if (rows[row] == null || rows[row].Length != width)
            {
                return false;
            }
        }

        return true;
    }

    private static string[] CloneRows(string[] rows)
    {
        string[] clone = new string[rows.Length];

        for (int row = 0; row < rows.Length; row++)
        {
            clone[row] = rows[row] ?? "";
        }

        return clone;
    }
}
