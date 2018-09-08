# -*- coding: utf-8 -*-


import random
import string

from core4.service.access.handler import BaseHandler

#: password length used to create MongoDB access
PASSWORD_LENGTH = 32


class MongoHandler(BaseHandler):

    """
    This class handles MongoDB access permissions. The handler is registered
    in :attr:core4.service.access.manager.HANDLER` attribute.

    The handler provides read-only access to MongoDB databases specified by
    the user/role permission field (``mongodb://<DBNAME>``). Additionally the
    handler creates user user database at ``user!<USERNAME>`` with the built-in
    ``dbOwner`` role assigned to the user.

    .. note:: The prefix of the user database (``user!``) can be defined with
              core4 configuration option ``sys.userdb``.
    """
    def __init__(self, role, *args, **kwargs):
        super().__init__(role, *args, **kwargs)
        self.token = None
        self.admin_db = self.config.sys.admin.connection[
            self.config.sys.admin.database]

    def create_token(self):
        """
        This method creates a random password of length :attr:`PASSWORD_LENGTH`
        for the user to access the database

        :return: token/password (str)
        """
        if self.token is None:
            token = ''.join(
                random.SystemRandom().choice(
                    string.ascii_uppercase + string.digits) for _
                in range(PASSWORD_LENGTH))
            self.token = token
        return self.token

    def del_role(self):
        """
        This method deletes the MongoDB user and role if exist.
        """
        username = self.role.name
        user_info = self.admin_db.command("usersInfo", username)
        if user_info["users"]:
            self.admin_db.command('dropUser', username)
            self.logger.info('removed mongo user [%s]', username)
        else:
            self.logger.debug("mongo user [%s] not found", username)

    def add_role(self):
        """
        This method creates the role and returns the token/password created by
        :meth:`.create_token`.

        .. note:: The user is created in MongoDB admin system database,
                  collection ``system.users``. Additionally the MongoDB
                  built-in role *dbOwner* is granted on database
                  ``user!<USERNAME>``. The prefix *user!* can be configured
                  with core4 configuration option ``sys.userdb``.

        :return: token/password (str)
        """
        # create the role
        username = self.role.name
        password = self.create_token()
        userdb = "".join([self.config.sys.userdb, username])
        self.admin_db.command('createUser', username, pwd=password, roles=[])
        self.admin_db.command(
            'grantRolesToUser', username,
            roles=[{'role': 'dbOwner', 'db': userdb}])
        self.logger.info(
            'created mongo user [%s] with private [%s]', username, userdb)
        return password

    def grant(self, database):
        # cascade db permission, then grant read-only access
        username = self.role.name
        self.admin_db.command(
            'grantRolesToUser', username,
            roles=[{'role': 'read', 'db': database}])
        self.logger.info('grant role [%s] access to [%s]', username, database)