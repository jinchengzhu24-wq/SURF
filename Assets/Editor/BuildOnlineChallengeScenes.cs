#if UNITY_EDITOR
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;
using UnityEngine.Tilemaps;
using UnityEngine.UI;

public static class BuildOnlineChallengeScenes
{
    private const string WaitingScenePath =
        "Assets/Scenes/Matchmaking/Online/Challenge_Waiting.unity";
    private const string OnlineLevelScenePath =
        "Assets/Scenes/Matchmaking/Online/Online_Level.unity";

    private static readonly Color OrangeBackground =
        new Color32(255, 195, 124, 255);
    private static readonly Color Ink = new Color32(52, 67, 70, 255);
    private static readonly Color Cream = new Color32(255, 240, 211, 248);
    private static readonly Color Orange = new Color32(224, 129, 56, 255);
    private static readonly Color Green = new Color32(67, 155, 91, 255);
    private static readonly Color Red = new Color32(181, 72, 63, 255);
    private static readonly Color Blue = new Color32(82, 104, 177, 255);

    private static Font pixelFont;

    [MenuItem("Tools/Online/Build Challenge Scenes")]
    public static void Build()
    {
        pixelFont = AssetDatabase.LoadAssetAtPath<Font>(
            "Assets/Font/Pixelnauts.ttf"
        );

        if (pixelFont == null)
        {
            throw new System.InvalidOperationException(
                "Pixelnauts font could not be loaded."
            );
        }

        BuildWaitingScene();
        BuildOnlineLevelScene();
        EnsureBuildSettings();
        AssetDatabase.SaveAssets();
        AssetDatabase.Refresh();
        Debug.Log("Online challenge scenes were rebuilt successfully.");
    }

    public static void BuildFromCommandLine()
    {
        Build();
    }

    public static void BuildWebGLFromCommandLine()
    {
        string[] enabledScenes = GetEnabledScenePaths();
        string outputPath = Path.Combine(
            Directory.GetParent(Application.dataPath).FullName,
            "WebGLBuild"
        );
        BuildPlayerOptions options = new BuildPlayerOptions
        {
            scenes = enabledScenes,
            locationPathName = outputPath,
            target = BuildTarget.WebGL,
            options = BuildOptions.None
        };
        BuildReport report = BuildPipeline.BuildPlayer(options);

        if (report.summary.result != BuildResult.Succeeded)
        {
            throw new System.InvalidOperationException(
                "WebGL build failed: " + report.summary.result
            );
        }

        Debug.Log(
            "WebGL build completed: "
            + report.summary.totalSize
            + " bytes"
        );
    }

    private static void BuildWaitingScene()
    {
        Scene scene = EditorSceneManager.NewScene(
            NewSceneSetup.EmptyScene,
            NewSceneMode.Single
        );
        CreateCamera(5f);
        InstantiatePrefab("Assets/Prefebs/Grid.prefab", "Grid");

        Canvas canvas = CreateCanvas();
        RectTransform root = CreateRect(
            "WaitingUIRoot",
            canvas.transform,
            Vector2.zero,
            Vector2.zero
        );
        Stretch(root);

        CreateText(
            "TitleText",
            root,
            "CHALLENGE WAITING",
            62,
            Ink,
            new Vector2(0f, 365f),
            new Vector2(1200f, 90f),
            TextAnchor.MiddleCenter
        );
        CreateText(
            "SubtitleText",
            root,
            "YOUR LEVEL IS LOCKED IN. KEEP THIS WINDOW OPEN.",
            24,
            Ink,
            new Vector2(0f, 300f),
            new Vector2(1200f, 55f),
            TextAnchor.MiddleCenter
        );

        Image card = CreatePanel(
            "WaitingStatusCard",
            root,
            Cream,
            new Vector2(0f, 45f),
            new Vector2(1100f, 360f)
        );
        CreatePanel(
            "CardAccent",
            card.rectTransform,
            Orange,
            new Vector2(0f, 160f),
            new Vector2(1100f, 40f)
        );
        CreateText(
            "StatusLabel",
            card.rectTransform,
            "MATCH STATUS",
            24,
            Orange,
            new Vector2(0f, 105f),
            new Vector2(900f, 50f),
            TextAnchor.MiddleCenter
        );
        Text statusText = CreateText(
            "ChallengeStatusText",
            card.rectTransform,
            "SUBMITTING YOUR CHALLENGE...",
            36,
            Ink,
            new Vector2(0f, 15f),
            new Vector2(940f, 125f),
            TextAnchor.MiddleCenter
        );
        Text roomText = CreateText(
            "RoomCodeText",
            card.rectTransform,
            "ROOM ------",
            25,
            Blue,
            new Vector2(0f, -105f),
            new Vector2(800f, 50f),
            TextAnchor.MiddleCenter
        );

        Button playButton = CreateButton(
            "PlayOpponentButton",
            root,
            "PlayOpponentButtonText",
            "PLAY OPPONENT LEVEL",
            Green,
            new Vector2(0f, -235f),
            new Vector2(570f, 92f),
            31
        );
        playButton.interactable = false;
        Button leaveButton = CreateButton(
            "LeaveMatchButton",
            root,
            "LeaveMatchButtonText",
            "LEAVE MATCH",
            Red,
            new Vector2(0f, -345f),
            new Vector2(350f, 72f),
            25
        );

        GameObject controllerObject = new GameObject(
            "ChallengeWaitingController"
        );
        ChallengeWaitingController controller =
            controllerObject.AddComponent<ChallengeWaitingController>();
        SerializedObject serializedController = new SerializedObject(controller);
        serializedController.FindProperty("statusText").objectReferenceValue =
            statusText;
        serializedController.FindProperty("roomCodeText").objectReferenceValue =
            roomText;
        serializedController.FindProperty("playButton").objectReferenceValue =
            playButton;
        serializedController.FindProperty("playButtonText").objectReferenceValue =
            playButton.GetComponentInChildren<Text>();
        serializedController.FindProperty("leaveButton").objectReferenceValue =
            leaveButton;
        serializedController.ApplyModifiedPropertiesWithoutUndo();

        CreateEventSystem();
        EditorSceneManager.SaveScene(scene, WaitingScenePath);
    }

    private static void BuildOnlineLevelScene()
    {
        Scene scene = EditorSceneManager.NewScene(
            NewSceneSetup.EmptyScene,
            NewSceneMode.Single
        );
        CreateCamera(6f);

        GameObject grid = InstantiatePrefab(
            "Assets/Prefebs/Grid.prefab",
            "Grid"
        );
        GameObject levelDataObject = InstantiatePrefab(
            "Assets/Prefebs/LevelData.prefab",
            "LevelData"
        );
        GameObject levelLoaderObject = InstantiatePrefab(
            "Assets/Prefebs/LevelLoader.prefab",
            "LevelLoader"
        );
        GameObject levelManagerObject = InstantiatePrefab(
            "Assets/Prefebs/LevelManager.prefab",
            "LevelManager"
        );
        GameObject generatedRootObject = new GameObject("GeneratedLevel");

        LevelData levelData = levelDataObject.GetComponent<LevelData>();
        LevelSolver levelSolver = levelDataObject.GetComponent<LevelSolver>();
        LevelGenerator levelGenerator =
            levelDataObject.GetComponent<LevelGenerator>();

        if (levelGenerator != null)
        {
            Object.DestroyImmediate(levelGenerator);
        }

        if (levelSolver == null)
        {
            levelSolver = levelDataObject.AddComponent<LevelSolver>();
        }

        LevelLoader levelLoader =
            levelLoaderObject.GetComponent<LevelLoader>();
        LevelManager levelManager =
            levelManagerObject.GetComponent<LevelManager>();
        Dictionary<string, Tilemap> tilemaps =
            FindTilemapsByName(grid);

        levelLoader.levelData = levelData;
        levelLoader.levelManager = levelManager;
        levelLoader.levelGenerator = null;
        levelLoader.llmClient = null;
        levelLoader.generateBeforeLoad = false;
        levelLoader.useLLMPlan = false;
        levelLoader.deferInitialLLMLoad = false;
        levelLoader.deferLoadToExternalController = true;
        levelLoader.levelRoot = generatedRootObject.transform;
        levelLoader.groundTilemap = tilemaps["Base"];
        levelLoader.wallTilemap = tilemaps["Wall"];
        levelLoader.waterTilemap = tilemaps["Water"];

        levelManager.levelLoader = levelLoader;
        levelManager.completeAction = LevelManager.CompleteAction.StayInCurrentScene;
        levelManager.allowRestartWithR = true;
        levelSolver.levelData = levelData;
        levelSolver.parseOnStart = false;
        levelSolver.solveOnStart = false;
        levelSolver.maxSearchStates = 300000;

        Canvas canvas = CreateCanvas();
        RectTransform root = CreateRect(
            "OnlineLevelUIRoot",
            canvas.transform,
            Vector2.zero,
            Vector2.zero
        );
        Stretch(root);

        Image header = CreatePanel(
            "OnlineLevelHeader",
            root,
            new Color32(255, 240, 211, 235),
            new Vector2(0f, 455f),
            new Vector2(1920f, 115f)
        );
        CreatePanel(
            "HeaderAccent",
            header.rectTransform,
            Blue,
            new Vector2(0f, -52f),
            new Vector2(1920f, 11f)
        );
        CreateText(
            "OnlineLevelTitle",
            header.rectTransform,
            "OPPONENT CHALLENGE",
            38,
            Ink,
            new Vector2(-560f, 5f),
            new Vector2(650f, 70f),
            TextAnchor.MiddleLeft
        );
        Text roomText = CreateText(
            "RoomCodeText",
            header.rectTransform,
            "ROOM ------",
            23,
            Blue,
            new Vector2(250f, 5f),
            new Vector2(360f, 60f),
            TextAnchor.MiddleCenter
        );
        CreateText(
            "RestartHint",
            header.rectTransform,
            "PRESS R TO RESTART",
            20,
            Ink,
            new Vector2(555f, 5f),
            new Vector2(350f, 60f),
            TextAnchor.MiddleCenter
        );
        Button leaveButton = CreateButton(
            "LeaveMatchButton",
            header.rectTransform,
            "LeaveMatchButtonText",
            "LEAVE",
            Red,
            new Vector2(820f, 5f),
            new Vector2(180f, 65f),
            22
        );

        Image blackPanel = CreatePanel(
            "BlackPanel",
            root,
            Color.black,
            Vector2.zero,
            Vector2.zero
        );
        Stretch(blackPanel.rectTransform);
        blackPanel.raycastTarget = false;
        levelManager.blackPanel = blackPanel;

        Image statusPanelImage = CreatePanel(
            "OnlineLevelStatusPanel",
            root,
            Cream,
            new Vector2(0f, 0f),
            new Vector2(950f, 250f)
        );
        CreatePanel(
            "StatusAccent",
            statusPanelImage.rectTransform,
            Orange,
            new Vector2(0f, 105f),
            new Vector2(950f, 40f)
        );
        Text statusText = CreateText(
            "OnlineLevelStatusText",
            statusPanelImage.rectTransform,
            "VERIFYING OPPONENT CHALLENGE...",
            33,
            Ink,
            new Vector2(0f, -10f),
            new Vector2(820f, 130f),
            TextAnchor.MiddleCenter
        );

        Image completePanelImage = CreatePanel(
            "ChallengeCompletePanel",
            root,
            Cream,
            new Vector2(0f, 0f),
            new Vector2(1000f, 390f)
        );
        CreatePanel(
            "CompleteAccent",
            completePanelImage.rectTransform,
            Green,
            new Vector2(0f, 175f),
            new Vector2(1000f, 40f)
        );
        CreateText(
            "CompleteTitle",
            completePanelImage.rectTransform,
            "CHALLENGE COMPLETE",
            49,
            Green,
            new Vector2(0f, 60f),
            new Vector2(850f, 100f),
            TextAnchor.MiddleCenter
        );
        CreateText(
            "CompleteSubtitle",
            completePanelImage.rectTransform,
            "YOU SOLVED YOUR OPPONENT'S LEVEL.",
            27,
            Ink,
            new Vector2(0f, -40f),
            new Vector2(850f, 80f),
            TextAnchor.MiddleCenter
        );
        CreateText(
            "CompleteHint",
            completePanelImage.rectTransform,
            "RESULT SYNC WILL BE ADDED IN THE NEXT STAGE.",
            20,
            Blue,
            new Vector2(0f, -55f),
            new Vector2(850f, 60f),
            TextAnchor.MiddleCenter
        );
        Button completeLeaveButton = CreateButton(
            "CompleteLeaveMatchButton",
            completePanelImage.rectTransform,
            "CompleteLeaveMatchButtonText",
            "LEAVE MATCH",
            Red,
            new Vector2(0f, -135f),
            new Vector2(330f, 65f),
            23
        );
        completePanelImage.gameObject.SetActive(false);

        GameObject controllerObject = new GameObject("OnlineLevelController");
        OnlineLevelController controller =
            controllerObject.AddComponent<OnlineLevelController>();
        SerializedObject serializedController = new SerializedObject(controller);
        serializedController.FindProperty("levelData").objectReferenceValue =
            levelData;
        serializedController.FindProperty("levelLoader").objectReferenceValue =
            levelLoader;
        serializedController.FindProperty("levelManager").objectReferenceValue =
            levelManager;
        serializedController.FindProperty("levelSolver").objectReferenceValue =
            levelSolver;
        serializedController.FindProperty("roomCodeText").objectReferenceValue =
            roomText;
        serializedController.FindProperty("statusText").objectReferenceValue =
            statusText;
        serializedController.FindProperty("statusPanel").objectReferenceValue =
            statusPanelImage.gameObject;
        serializedController.FindProperty("completePanel").objectReferenceValue =
            completePanelImage.gameObject;
        serializedController.FindProperty("leaveButton").objectReferenceValue =
            leaveButton;
        serializedController
            .FindProperty("completeLeaveButton")
            .objectReferenceValue = completeLeaveButton;
        serializedController.ApplyModifiedPropertiesWithoutUndo();

        CreateEventSystem();
        EditorSceneManager.SaveScene(scene, OnlineLevelScenePath);
    }

    private static Camera CreateCamera(float orthographicSize)
    {
        GameObject cameraObject = new GameObject(
            "Main Camera",
            typeof(Camera),
            typeof(AudioListener)
        );
        cameraObject.tag = "MainCamera";
        Camera camera = cameraObject.GetComponent<Camera>();
        camera.orthographic = true;
        camera.orthographicSize = orthographicSize;
        camera.backgroundColor = OrangeBackground;
        camera.clearFlags = CameraClearFlags.SolidColor;
        cameraObject.transform.position = new Vector3(0f, 1f, -10f);
        return camera;
    }

    private static Canvas CreateCanvas()
    {
        GameObject canvasObject = new GameObject(
            "Canvas",
            typeof(RectTransform),
            typeof(Canvas),
            typeof(CanvasScaler),
            typeof(GraphicRaycaster)
        );
        Canvas canvas = canvasObject.GetComponent<Canvas>();
        canvas.renderMode = RenderMode.ScreenSpaceOverlay;
        CanvasScaler scaler = canvasObject.GetComponent<CanvasScaler>();
        scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
        scaler.referenceResolution = new Vector2(1920f, 1080f);
        scaler.matchWidthOrHeight = 0.5f;
        return canvas;
    }

    private static RectTransform CreateRect(
        string name,
        Transform parent,
        Vector2 anchoredPosition,
        Vector2 size)
    {
        GameObject gameObject = new GameObject(
            name,
            typeof(RectTransform)
        );
        RectTransform rect = gameObject.GetComponent<RectTransform>();
        rect.SetParent(parent, false);
        rect.anchorMin = new Vector2(0.5f, 0.5f);
        rect.anchorMax = new Vector2(0.5f, 0.5f);
        rect.pivot = new Vector2(0.5f, 0.5f);
        rect.anchoredPosition = anchoredPosition;
        rect.sizeDelta = size;
        return rect;
    }

    private static void Stretch(RectTransform rect)
    {
        rect.anchorMin = Vector2.zero;
        rect.anchorMax = Vector2.one;
        rect.offsetMin = Vector2.zero;
        rect.offsetMax = Vector2.zero;
    }

    private static Image CreatePanel(
        string name,
        Transform parent,
        Color color,
        Vector2 anchoredPosition,
        Vector2 size)
    {
        RectTransform rect = CreateRect(
            name,
            parent,
            anchoredPosition,
            size
        );
        Image image = rect.gameObject.AddComponent<Image>();
        image.color = color;
        return image;
    }

    private static Text CreateText(
        string name,
        Transform parent,
        string value,
        int fontSize,
        Color color,
        Vector2 anchoredPosition,
        Vector2 size,
        TextAnchor alignment)
    {
        RectTransform rect = CreateRect(
            name,
            parent,
            anchoredPosition,
            size
        );
        Text text = rect.gameObject.AddComponent<Text>();
        text.font = pixelFont;
        text.fontSize = fontSize;
        text.fontStyle = FontStyle.Normal;
        text.alignment = alignment;
        text.color = color;
        text.text = value;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Overflow;
        text.raycastTarget = false;
        return text;
    }

    private static Button CreateButton(
        string name,
        Transform parent,
        string textName,
        string label,
        Color color,
        Vector2 anchoredPosition,
        Vector2 size,
        int fontSize)
    {
        Image image = CreatePanel(
            name,
            parent,
            color,
            anchoredPosition,
            size
        );
        Button button = image.gameObject.AddComponent<Button>();
        button.targetGraphic = image;
        ColorBlock colors = button.colors;
        colors.normalColor = Color.white;
        colors.highlightedColor = new Color(1f, 1f, 1f, 0.85f);
        colors.pressedColor = new Color(0.78f, 0.78f, 0.78f, 1f);
        colors.disabledColor = new Color(0.5f, 0.5f, 0.5f, 0.72f);
        colors.colorMultiplier = 1f;
        button.colors = colors;

        Text text = CreateText(
            textName,
            image.rectTransform,
            label,
            fontSize,
            Color.white,
            Vector2.zero,
            size,
            TextAnchor.MiddleCenter
        );
        Stretch(text.rectTransform);
        return button;
    }

    private static GameObject InstantiatePrefab(string path, string name)
    {
        GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(path);

        if (prefab == null)
        {
            throw new System.InvalidOperationException(
                "Prefab could not be loaded: " + path
            );
        }

        GameObject instance =
            PrefabUtility.InstantiatePrefab(prefab) as GameObject;
        instance.name = name;
        return instance;
    }

    private static Dictionary<string, Tilemap> FindTilemapsByName(
        GameObject root)
    {
        Dictionary<string, Tilemap> result =
            new Dictionary<string, Tilemap>();
        Tilemap[] tilemaps = root.GetComponentsInChildren<Tilemap>(true);

        for (int i = 0; i < tilemaps.Length; i++)
        {
            result[tilemaps[i].gameObject.name] = tilemaps[i];
        }

        foreach (string requiredName in new[] { "Base", "Wall", "Water" })
        {
            if (!result.ContainsKey(requiredName))
            {
                throw new System.InvalidOperationException(
                    "Grid prefab is missing tilemap " + requiredName
                );
            }
        }

        return result;
    }

    private static void CreateEventSystem()
    {
        new GameObject(
            "EventSystem",
            typeof(EventSystem),
            typeof(StandaloneInputModule)
        );
    }

    private static void EnsureBuildSettings()
    {
        List<EditorBuildSettingsScene> scenes =
            new List<EditorBuildSettingsScene>(
                EditorBuildSettings.scenes
            );
        AddOrEnableScene(scenes, WaitingScenePath);
        AddOrEnableScene(scenes, OnlineLevelScenePath);
        EditorBuildSettings.scenes = scenes.ToArray();
    }

    private static string[] GetEnabledScenePaths()
    {
        List<string> paths = new List<string>();
        EditorBuildSettingsScene[] scenes = EditorBuildSettings.scenes;

        for (int i = 0; i < scenes.Length; i++)
        {
            if (scenes[i].enabled)
            {
                paths.Add(scenes[i].path);
            }
        }

        return paths.ToArray();
    }

    private static void AddOrEnableScene(
        List<EditorBuildSettingsScene> scenes,
        string path)
    {
        for (int i = 0; i < scenes.Count; i++)
        {
            if (scenes[i].path == path)
            {
                scenes[i] = new EditorBuildSettingsScene(path, true);
                return;
            }
        }

        scenes.Add(new EditorBuildSettingsScene(path, true));
    }
}
#endif
