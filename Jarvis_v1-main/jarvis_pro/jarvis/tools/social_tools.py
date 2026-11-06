"""Social media through the official APIs.

X (Twitter) : X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
Facebook    : META_PAGE_ID, META_PAGE_ACCESS_TOKEN
Instagram   : META_IG_USER_ID, META_PAGE_ACCESS_TOKEN  (Business/Creator account linked to the Page)
LinkedIn    : LINKEDIN_ACCESS_TOKEN, LINKEDIN_PERSON_ID
"""

from __future__ import annotations

import asyncio

import httpx
from livekit.agents import RunContext, ToolError, function_tool

from .. import config
from ..guard import action

GRAPH = f"https://graph.facebook.com/{config.env('META_GRAPH_VERSION', 'v23.0')}"


async def _graph(method: str, path: str, **params) -> dict:
    token = config.require_env("META_PAGE_ACCESS_TOKEN")["META_PAGE_ACCESS_TOKEN"]
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.request(method, f"{GRAPH}/{path}", params={**params, "access_token": token})
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise ToolError(f"Meta API error: {data.get('error', {}).get('message', r.text)[:200]}")
    return data


# ---------------------------------------------------------------- X ----------
@function_tool
async def post_to_x(context: RunContext, text: str, confirmed: bool = False) -> str:
    """Publish a post on Boss's X (Twitter) account.

    Args:
        text: The post, max 280 characters.
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    if len(text) > 280:
        raise ToolError(f"Post is {len(text)} characters; X allows 280. Shorten it.")
    keys = config.require_env("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    async with action(context, "social.x.post", f"Post on X: {text}", {"text": text}, confirmed) as act:
        import tweepy

        x = tweepy.Client(consumer_key=keys["X_API_KEY"], consumer_secret=keys["X_API_SECRET"],
                          access_token=keys["X_ACCESS_TOKEN"], access_token_secret=keys["X_ACCESS_TOKEN_SECRET"])
        resp = await asyncio.to_thread(x.create_tweet, text=text)
        act["result"] = resp.data.get("id")
    return "Posted on X."


# ---------------------------------------------------------- Facebook ---------
@function_tool
async def facebook_post(context: RunContext, message: str, link: str = "", confirmed: bool = False) -> str:
    """Publish a post on Boss's Facebook Page.

    Args:
        message: Post text.
        link: Optional URL to attach.
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    page = config.require_env("META_PAGE_ID")["META_PAGE_ID"]
    async with action(context, "social.facebook.post", f"Facebook post: {message[:120]}",
                      {"message": message, "link": link}, confirmed) as act:
        params = {"message": message, **({"link": link} if link else {})}
        act["result"] = (await _graph("POST", f"{page}/feed", **params)).get("id")
    return "Posted to the Facebook Page."


@function_tool
async def facebook_recent_comments(context: RunContext, posts: int = 3) -> str:
    """Read recent comments on Boss's Facebook Page posts (includes comment ids for replying).

    Args:
        posts: How many recent posts to check (1-10).
    """
    page = config.require_env("META_PAGE_ID")["META_PAGE_ID"]
    async with action(context, "social.facebook.read", "Read Facebook comments"):
        data = await _graph("GET", f"{page}/feed", limit=max(1, min(posts, 10)),
                            fields="message,created_time,comments.limit(10){id,from,message,created_time}")
    lines = []
    for post in data.get("data", []):
        lines.append(f"POST: {(post.get('message') or '(no text)')[:100]}")
        for c in post.get("comments", {}).get("data", []):
            who = (c.get("from") or {}).get("name", "someone")
            lines.append(f"  - [{c['id']}] {who}: {c.get('message', '')}")
    return "\n".join(lines) or "No posts found."


@function_tool
async def facebook_reply_comment(context: RunContext, comment_id: str, message: str, confirmed: bool = False) -> str:
    """Reply to a comment on the Facebook Page.

    Args:
        comment_id: Comment id from facebook_recent_comments.
        message: Reply text.
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    async with action(context, "social.facebook.reply", f"Reply to FB comment {comment_id}: {message[:120]}",
                      {"comment_id": comment_id, "message": message}, confirmed):
        await _graph("POST", f"{comment_id}/comments", message=message)
    return "Replied."


# --------------------------------------------------------- Instagram ---------
@function_tool
async def instagram_post_image(context: RunContext, image_url: str, caption: str, confirmed: bool = False) -> str:
    """Publish an image post on Boss's Instagram Business account.

    Args:
        image_url: Public https URL of a JPEG image.
        caption: Caption text with hashtags.
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    ig = config.require_env("META_IG_USER_ID")["META_IG_USER_ID"]
    async with action(context, "social.instagram.post", f"Instagram post: {caption[:120]}",
                      {"image_url": image_url, "caption": caption}, confirmed) as act:
        container = await _graph("POST", f"{ig}/media", image_url=image_url, caption=caption)
        for _ in range(10):  # wait for Instagram to process the image
            status = await _graph("GET", container["id"], fields="status_code")
            if status.get("status_code") == "FINISHED":
                break
            if status.get("status_code") == "ERROR":
                raise ToolError("Instagram could not process that image.")
            await asyncio.sleep(2)
        act["result"] = (await _graph("POST", f"{ig}/media_publish", creation_id=container["id"])).get("id")
    return "Published on Instagram."


@function_tool
async def instagram_recent_comments(context: RunContext, posts: int = 3) -> str:
    """Read recent comments on Boss's Instagram posts.

    Args:
        posts: How many recent posts to check (1-10).
    """
    ig = config.require_env("META_IG_USER_ID")["META_IG_USER_ID"]
    async with action(context, "social.instagram.read", "Read Instagram comments"):
        data = await _graph("GET", f"{ig}/media", limit=max(1, min(posts, 10)),
                            fields="caption,timestamp,comments.limit(10){username,text,timestamp}")
    lines = []
    for m in data.get("data", []):
        lines.append(f"POST: {(m.get('caption') or '(no caption)')[:100]}")
        for c in m.get("comments", {}).get("data", []):
            lines.append(f"  - @{c.get('username', '?')}: {c.get('text', '')}")
    return "\n".join(lines) or "No posts found."


# ---------------------------------------------------------- LinkedIn ---------
@function_tool
async def linkedin_post(context: RunContext, text: str, confirmed: bool = False) -> str:
    """Publish a text post on Boss's LinkedIn profile.

    Args:
        text: Post text (max 3000 characters).
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    keys = config.require_env("LINKEDIN_ACCESS_TOKEN", "LINKEDIN_PERSON_ID")
    body = {
        "author": f"urn:li:person:{keys['LINKEDIN_PERSON_ID']}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {"com.linkedin.ugc.ShareContent": {
            "shareCommentary": {"text": text[:3000]}, "shareMediaCategory": "NONE"}},
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    async with action(context, "social.linkedin.post", f"LinkedIn post: {text[:120]}", {"text": text}, confirmed):
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post("https://api.linkedin.com/v2/ugcPosts", json=body, headers={
                "Authorization": f"Bearer {keys['LINKEDIN_ACCESS_TOKEN']}",
                "X-Restli-Protocol-Version": "2.0.0"})
        if r.status_code >= 400:
            raise ToolError(f"LinkedIn API error {r.status_code}: {r.text[:200]}")
    return "Posted on LinkedIn."


TOOLS = [
    ("social.x.post", post_to_x),
    ("social.facebook.post", facebook_post),
    ("social.facebook.read", facebook_recent_comments),
    ("social.facebook.reply", facebook_reply_comment),
    ("social.instagram.post", instagram_post_image),
    ("social.instagram.read", instagram_recent_comments),
    ("social.linkedin.post", linkedin_post),
]
