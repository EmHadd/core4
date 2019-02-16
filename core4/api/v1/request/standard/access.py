"""
Implements core4 standard :class:`.AccessHandler`.
"""

from core4.api.v1.request.main import CoreRequestHandler
from core4.api.v1.request.role.access.manager import CoreAccessManager


class AccessHandler(CoreRequestHandler):
    title = "database access manager"
    author = "mra"

    async def post(self, protocol=None):
        """
        Request database access token for all or the specified access
        protocols. At the moment only ``mongodb`` access protocol is
        implemented.

        Methods:
            POST /

        Parameters:
            protocol (str): access protocol

        Returns:
            data element with

            - **mongodb** (*str*): the created authentication token

        Raises:
            401: Unauthorized

        Examples:
            >>> from requests import get, post
            >>> url = "http://localhost:5001/core4/api/v1/login"
            >>> rv = get(url + "?username=admin&password=hans")
            >>> token = rv.json()["data"]["token"]
            >>> rv = post("http://localhost:5001/coco/v1/access",
            >>>           headers={"Authorization": "Bearer " + token})
            >>> rv.json()
            {
                '_id': '5c6655f0ad70714b06cabe99',
                'code': 200,
                'data': {
                    'mongodb': 'ZZQ5BOXQ6IK49XM1TZ1LOSD9D09DP31O'
                },
                'message': 'OK',
                'timestamp': '2019-02-15T06:02:24.884558'
            }
            >>> rv = post("http://devops:5005/coco/v1/access/mongodb",
            >>>           headers={"Authorization": "Bearer " + token})
            >>> rv.json()
            {
                '_id': '5c665642ad70714b060bfd0b',
                'code': 200,
                'data': '1CHVGSRLDRKWPJEFFQ9BNOOY8WSIWGVU',
                'message': 'OK',
                'timestamp': '2019-02-15T06:03:46.891981'
            }
        """
        manager = CoreAccessManager(self.user)
        if protocol == "":
            body = await manager.synchronise_all()
        else:
            body = await manager.synchronise(protocol)
        return self.reply(body)