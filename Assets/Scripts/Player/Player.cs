using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Tilemaps;

public class Player : MonoBehaviour
{
    public float moveDistance = 1f;
    public bool inputEnabled = true;

    private readonly List<Tilemap> blockingTilemaps = new List<Tilemap>();

    private void Awake()
    {
        RefreshBlockingTilemaps();
    }

    private void Update()
    {
        if (!inputEnabled)
        {
            return;
        }

        Move();
    }

    public void SetInputEnabled(bool enabled)
    {
        inputEnabled = enabled;
    }

    private void Move()
    {
        Vector3 dir = Vector3.zero;

        if (Input.GetKeyDown(KeyCode.W))
        {
            dir = Vector3.up;
        }

        if (Input.GetKeyDown(KeyCode.S))
        {
            dir = Vector3.down;
        }

        if (Input.GetKeyDown(KeyCode.A))
        {
            dir = Vector3.left;
        }

        if (Input.GetKeyDown(KeyCode.D))
        {
            dir = Vector3.right;
        }

        if (dir == Vector3.zero)
        {
            return;
        }

        Vector3 nextPos = transform.position + dir * moveDistance;

        if (IsBlocked(nextPos))
        {
            return;
        }

        Box box = GetBox(nextPos);

        if (box != null)
        {
            Vector3 boxNextPos = box.transform.position + dir * moveDistance;

            if (IsBlocked(boxNextPos) || GetBox(boxNextPos) != null)
            {
                return;
            }

            box.Move(dir * moveDistance);
        }

        transform.position = nextPos;
    }

    private bool IsBlocked(Vector3 position)
    {
        if (IsBlockedByTilemap(position))
        {
            return true;
        }

        Collider2D[] hits = Physics2D.OverlapPointAll(position);

        for (int i = 0; i < hits.Length; i++)
        {
            if (hits[i].CompareTag("Wall") || hits[i].CompareTag("Water"))
            {
                return true;
            }
        }

        return false;
    }

    private bool IsBlockedByTilemap(Vector3 position)
    {
        if (blockingTilemaps.Count == 0)
        {
            RefreshBlockingTilemaps();
        }

        for (int i = 0; i < blockingTilemaps.Count; i++)
        {
            Tilemap tilemap = blockingTilemaps[i];

            if (tilemap == null)
            {
                continue;
            }

            Vector3Int cell = tilemap.WorldToCell(position);
            if (tilemap.HasTile(cell))
            {
                return true;
            }
        }

        return false;
    }

    private void RefreshBlockingTilemaps()
    {
        blockingTilemaps.Clear();

        Tilemap[] tilemaps = FindObjectsOfType<Tilemap>();

        for (int i = 0; i < tilemaps.Length; i++)
        {
            Tilemap tilemap = tilemaps[i];

            if (tilemap.CompareTag("Wall") || tilemap.CompareTag("Water"))
            {
                blockingTilemaps.Add(tilemap);
            }
        }
    }

    private Box GetBox(Vector3 position)
    {
        Collider2D[] hits = Physics2D.OverlapPointAll(position);

        for (int i = 0; i < hits.Length; i++)
        {
            Box box = hits[i].GetComponent<Box>();
            if (box != null)
            {
                return box;
            }
        }

        return null;
    }
}
