using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public class LevelManager : MonoBehaviour
{
    public enum CompleteAction
    {
        LoadNextScene,
        StayInCurrentScene,
        GenerateNewLevel
    }

    public PlayerAnimation anim;
    public LevelLoader levelLoader;

    [Header("Level Info")]
    public int boxCount;
    public int reachedCount;

    [Header("Black Panel")]
    public Image blackPanel;
    public float fadeTime = 1f;
    public float completeDelay = 1.5f;

    [Header("Complete Action")]
    public CompleteAction completeAction = CompleteAction.LoadNextScene;

    private void Start()
    {
        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        ResetLevelState();
        StartCoroutine(Fade(1, 0));
    }

    public void BoxReachTarget()
    {
        reachedCount++;

        if (reachedCount == boxCount)
        {
            StartCoroutine(CompleteLevel());
        }
    }

    public void BoxLeaveTarget()
    {
        reachedCount--;
    }

    public void ResetLevelState()
    {
        boxCount = FindObjectsOfType<Box>().Length;
        reachedCount = 0;
    }

    private IEnumerator CompleteLevel()
    {
        if (anim != null)
        {
            anim.Win();
        }

        yield return new WaitForSeconds(completeDelay);

        yield return Fade(0, 1);

        if (completeAction == CompleteAction.LoadNextScene)
        {
            LoadNextScene();
        }
        else if (completeAction == CompleteAction.GenerateNewLevel)
        {
            yield return GenerateNewLevel();
            yield return Fade(1, 0);
        }
        else
        {
            yield return Fade(1, 0);
        }
    }

    private IEnumerator GenerateNewLevel()
    {
        if (levelLoader == null)
        {
            levelLoader = FindObjectOfType<LevelLoader>();
        }

        if (levelLoader == null)
        {
            Debug.LogWarning("LevelManager: Cannot generate a new level because LevelLoader is missing.");
            yield break;
        }

        if (levelLoader.useLLMPlan)
        {
            yield return levelLoader.GenerateAndReloadWithLLMPlanRoutine();
        }
        else
        {
            levelLoader.GenerateAndReload();
        }
    }

    private void LoadNextScene()
    {
        int currentIndex = SceneManager.GetActiveScene().buildIndex;
        int nextIndex = currentIndex + 1;

        if (nextIndex >= SceneManager.sceneCountInBuildSettings)
        {
            Debug.LogWarning("LevelManager: No next scene in Build Settings.");
            return;
        }

        SceneManager.LoadScene(nextIndex);
    }

    private IEnumerator Fade(float startAlpha, float endAlpha)
    {
        if (blackPanel == null)
        {
            yield break;
        }

        float timer = 0;

        while (timer < fadeTime)
        {
            timer += Time.deltaTime;

            Color color = blackPanel.color;
            color.a = Mathf.Lerp(startAlpha, endAlpha, timer / fadeTime);
            blackPanel.color = color;

            yield return null;
        }

        Color finalColor = blackPanel.color;
        finalColor.a = endAlpha;
        blackPanel.color = finalColor;
    }
}
