import re
from flask import request, g, jsonify, url_for
from . import api
from .utils import login_required, json_required
from .errors import forbidden, unauthorized, bad_request, not_found
from app import db, rd, rank
from app.models import *
from sqlalchemy.sql import text
import app.cache as Cache
import app.cache.redis_keys as Keys

TOPICREGEX = re.compile(r"#([\s\S]+?)#")

@api.route('/status', methods=['POST'])
@json_required
@login_required
def create_status():
    """发表动态
    用户动态:
    json = {
        type: USER_STATUS,
        text: 非空字符串,
        pics: [String], 可选,
    }
    团体微博:
    json = {
        type: GROUP_STATUS,
        text: 非空字符串,
        group_id: 团体id,
        pics: [String], 可选,
    }
    团体帖子:
    json = {
        type: GROUP_POST,
        text: 非空字符串,
        title: 非空字符串,
        group_id: 团体id,
        pics: [String], 可选,
    }
    """
    type = request.json.get('type', -1)
    text = request.json.get('text', '')
    title = request.json.get('title', None)
    group_id = request.json.get('group_id', None)
    pics = request.json.get('pics', [])

    if text == '':
        return bad_request('text empty')

    s = None
    # 用户动态:
    if type=='USER_STATUS':
        s = Status(type=type, user=g.user, text=text)
        # 检查话题
        topics = list(set(TOPICREGEX.findall(text)))
        for topic in topics:
            t = Topic.query.filter_by(topic=topic).first()
            if t is None:
                t = Topic(topic=topic)
            t.statuses.append(s)
            db.session.add(t)


    # 团体微博:
    if type=='GROUP_STATUS':
        group = Group.query.get(group_id)
        if group is None:
            return bad_request('该团体不存在')
        s = Status(type=type, user=g.user,
                   group=group, text=text)

    # 团体帖子:
    if type=='GROUP_POST':
        group = Group.query.get(group_id)
        if title == '':
            return bad_request('title empty')
        if group is None:
            return bad_request('该团体不存在')
        s = Status(type=type, user=g.user,
                   group=group, title=title, text=text)

    if s is not None:
        for index, pic_url in enumerate(pics):
            p = StatusPicture(url=pic_url, status=s, index=index)
            db.session.add(p)
        db.session.add(s)
        db.session.commit()
        rank.push(s)
        return jsonify(s.to_json()), 201, \
            {'Location': url_for('api.get_status', id=s.id, _external=True)}

    return bad_request('参数有误')


@api.route('/status', methods=['GET'])
def get_status():
    """ 获取动态
    分类:
    1. 获取某条特定动态, 团体微博, 团体帖子
        params = { id }
    3. 获取个人微博(时间序)
        params = {
            type: user,
            user_id:
        }
    2. 获取团体微博(时间序)
        params = {
            type: group_status,
            group_id:
        }
    2. 获取用户动态(时间序)
        params = {
            type: status,
        }
    3. 获取团体帖子(热门序)
        params = {
            type: post,
            group_id = Integer
        }
    4. 获取推荐(热门序)
        params = {
            type: timeline,
        }
    5. 获取关注微博(时间序)
        params = {
            type: followed,
        }
    6. 获取话题下的微博:
        params = {
            type: topic,
            topic_name: //
        }
    公共参数
        limit: 可选, default=10
    Note:
    1. 根据offset排序的小问题:
        a. 当某条内容上升到offset之前, 用户可能错过, 忽略不计,
        b. 当某条内容下降到offset之后, 用户可能重新刷到, 客户端需要处理重叠
    2. 当返回空数组时, 代表没有更多内容, 客户端自行处理
    """
    id = request.args.get('id', -1, type=int)
    type = request.args.get('type', '')
    user_id = request.args.get('user_id', -1, type=int)
    group_id = request.args.get('group_id', -1, type=int)
    only_with_comment = request.args.get('only_with_comment', "")
    topic = request.args.get('topic', "")
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', 10, type=int)

    if id != -1:
        return jsonify(Cache.get_status_json(id))

    if type == 'user':
        u = Cache.get_user(user_id)
        if u is None:
            return not_found('找不到该用户')
        ss = Status.query.filter_by(user=u)
        ss = ss.order_by(Status.timestamp.desc())
        ss = ss.offset(offset).limit(limit)
        ss = [s.to_json() for s in ss]
        return jsonify(ss)

    if type == 'group_status':
        group = Group.query.get(group_id)
        if group is None:
            return not_found('找不到该团体')
        ss = Status.query.filter_by(group=group, type_id=Status.TYPES['GROUP_STATUS'])
        ss = ss.order_by(Status.timestamp.desc())
        ss = ss.offset(offset).limit(limit)
        ss = [s.to_json() for s in ss]
        return jsonify(ss)

    if type == "status":
        ss = Status.query.filter_by(type_id=Status.TYPES['USER_STATUS'])
        ss = ss.order_by(Status.id.desc())
        ss = ss.offset(offset).limit(limit)
        ss = [s.to_json() for s in ss]
        return jsonify(ss)

    if type == 'post':
        ss = Status.query.filter_by(type_id=Status.TYPES['GROUP_POST'])
        if group_id != -1:
            ss = ss.filter_by(group_id=group_id)
        ss = ss.order_by(Status.timestamp.desc())
        ss = ss.offset(offset).limit(limit)
        ss = [s.to_json() for s in ss]
        return jsonify(ss)

    if type == 'hot':
        ss = Status.query.order_by(Status.timestamp.desc())
        ss = ss.offset(offset).limit(limit)
        ss = [s.to_json() for s in ss]
        rank.get_fresh()
        return jsonify(ss)

    if type == 'timeline':
        ### TODO: with entities might be useful here
        if not hasattr(g, 'user'):
            return jsonify([])
        sql2 = """
        select * from (
            select 0 as kind, id, timestamp
            from statuses where user_id=:UID or user_id in (
                select followed_id from user_follows as F where F.follower_id=:UID
            )
            union
            select 1 as kind, id, timestamp
            from articles where official_account_id in (
                select official_account_id from subscriptions as S where S.users_id=:UID
            )
        ) as t order by timestamp DESC limit :LIMIT offset :OFFSET;
        """ # `S.users_id` because there is a typo in column name
        result = db.engine.execute(text(sql2), UID=g.user.id,
                LIMIT=limit, OFFSET=offset)
        result = list(result)
        status_ids = [ item['id'] for item in result
                if item['kind'] == 0]
        article_ids = [ item['id'] for item in result
                if item['kind'] == 1 ]
        statuses = Status.query.filter(Status.id.in_(
            status_ids)).all()
        articles = Article.query.filter(Article.id.in_(
            article_ids)).all()
        res = statuses + articles
        res = sorted(res, key=lambda x: x.timestamp, reverse=True)
        res = [item.to_json() for item in res]
        return jsonify(res)


    #if type == "trending":
        #ids = rank.get_mixed()[offset:offset+limit]
        #ss = [Status.query.get(id).to_json() for id in ids]
        #return jsonify(ss)


    if type == 'topic':
        key = Keys.topic_id.format(topic_name=topic)
        data = rd.get(key)
        if data != None:
            topic_id = data.decode()
        else:
            t = Topic.query.filter_by(topic=topic).first()
            if t is None:
                return jsonify([])
            topic_id = t.id
            rd.set(key, topic_id, Keys.topic_id_expire)
        sql = """
            select status_id from status_topic
            where topic_id=:TOPIC_ID
            order by status_id DESC limit :LIMIT offset :OFFSET;
        """
        result = db.engine.execute(text(sql), TOPIC_ID=topic_id,
                OFFSET=offset, LIMIT=limit)
        result = list(result)
        status_ids = [item['status_id'] for item in result]
        statuses = Cache.multiget_status_json(status_ids)
        res_map = {}
        for s in statuses:
            res_map[s['id']] = s
        res = [ res_map[id] for id in status_ids ]
        return jsonify(res)

    return bad_request('参数有误')


@api.route('/status', methods=['DELETE'])
@login_required
def delete_status():
    """ 删除微博, 成功返回删除的id """
    id = request.args.get('id', -1, type=int)
    if id == -1:
        return bad_request('id empty')
    s = Status.query.get_or_404(id)
    if s.user != g.user:
        return forbidden('owner required')
    db.session.delete(s)
    db.session.commit()
    rank.remove(s)
    return jsonify({'id': id, 'message': 'delete success'})


"""
动态点赞API
"""
@api.route('/status/like', methods=['POST'])
@json_required
@login_required
def create_status_like():
    """根据动态id为其点赞
    json = {
        "id": 动态id
    }
    """
    id = request.json.get('id', -1)
    s = Status.query.get_or_404(id)
    if g.user in s.liked_users:
        return jsonify({'id': id, 'message': 'already created.'})
    s.liked_users.append(g.user)
    db.session.add(s)
    db.session.commit()
    rank.push(s)
    return jsonify({'id': id, 'message': 'create success'})


@api.route('/status/like', methods=['DELETE'])
@login_required
def delete_status_like():
    ''' 根据id取消点赞
    '''
    id = request.args.get('id', -1, type=int)
    s = Status.query.get(id)
    if s is None:
        return not_found('该动态不存在或者已被删除')
    if g.user not in s.liked_users:
        return jsonify({'id': id, 'message': 'already deleted.'})
    s.liked_users.remove(g.user)
    db.session.add(s)
    db.session.commit()
    return jsonify({'id': id, 'message': 'delete success'})


"""
动态回复API
"""
@api.route('/status/reply', methods=['POST'])
@json_required
@login_required
def create_status_reply():
    status_id = request.json.get('status_id', -1)
    text = request.json.get('text', '')
    if text == '':
        return bad_request('text empty')
    s = Status.query.get_or_404(status_id)
    r = StatusReply(text=text, status=s, user=g.user)
    db.session.add(r)
    db.session.commit()
    rank.push(s)
    return jsonify(r.to_json()), 201, \
        {'Location': url_for('api.get_status_reply', id=r.id, _external=True)}


@api.route('/status/reply', methods=['GET'])
def get_status_reply():
    """根据指定条件获取动态
        请求参数:
        Filter(s):
        1. reverse=True/False, default is false and sort by timestamp.
        2. offset: 可选, 默认为0
        3. limit: 可选, 默认为10
        Note:
        2. 不符合条件时, 返回空数组
        3. 如果是逆序浏览, 有新的内容时, 不会提示
    """
    id = request.args.get('id', -1, type=int)
    status_id = request.args.get('status_id', -1, type=int)
    offset = request.args.get('offset', 0, type=int)
    reverse = request.args.get('reverse', False, type=bool)
    limit = request.args.get('limit', 10, type=int)
    if id != -1:
        reply = StatusReply.query.get_or_404(id)
        return jsonify(reply.to_json())
    s = Status.query.get_or_404(status_id)
    replies = s.replies
    replies = (replies.order_by(StatusReply.timestamp.desc())
               if reverse == 'true' else
               replies.order_by(StatusReply.timestamp))
    replies = replies.offset(offset).limit(limit)
    replies = [r.to_json() for r in replies]
    return jsonify(replies)


@api.route('/status/reply', methods=['DELETE'])
@login_required
def delete_status_reply():
    """ 删除微博, 成功返回删除的id """
    # id // status_reply id
    id = request.args.get('id', -1, type=int)
    if id == -1:
        return bad_request('id empty')
    r = StatusReply.query.get_or_404(id)
    if r.user != g.user:
        return forbidden('owner required')
    db.session.delete(r)
    db.session.commit()
    return jsonify({'id': id, 'message': 'delete success'})


"""
回复点赞API
"""
@api.route('/status/reply/like', methods=['POST'])
@json_required
@login_required
def create_status_reply_like():
    """根据回复id为其点赞
    json = {
        "id": 回复id
    }
    """
    id = request.json.get('id', -1)
    r = StatusReply.query.get_or_404(id)
    if g.user in r.liked_users:
        return jsonify({'id': r.id, 'message': 'already created.'})
    r.liked_users.append(g.user)
    db.session.add(r)
    db.session.commit()
    return jsonify({'id': r.id, 'message': 'create success'})


@api.route('/status/reply/like', methods=['DELETE'])
@login_required
def delete_status_reply_like():
    ''' 根据回复id取消点赞
    '''
    id = request.args.get('id', -1, type=int)
    r = StatusReply.query.get_or_404(id)
    if g.user not in r.liked_users:
        return jsonify({'id': r.id, 'message': 'already deleted.'})
    r.liked_users.remove(g.user)
    db.session.add(r)
    db.session.commit()
    return jsonify({'id': r.id, 'message': 'delete success'})

@api.route('/topic', methods=['GET'])
def get_topic():
    id = request.args.get('id', -1, type=int)
    topic = request.args.get('topic', '')
    if id != -1:
        t = Topic.query.get_or_404(id)
        return jsonify(t.to_json())
    if topic != '':
        t = Topic.query.filter_by(topic=topic).first_or_404()
        return jsonify(t.to_json())
    return bad_request("参数有误")
